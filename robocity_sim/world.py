"""The endless world substrate — a faithful port of
``game/modules/robot_city/world.go``.

Everything here is a pure function of ``(seed, x, y)`` so the same seed always
yields the same map, matching the server engine exactly.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .config import Config, BUILDING_BASE

MASK64 = 0xFFFFFFFFFFFFFFFF


def _u64(n: int) -> int:
    """Reinterpret an int as an unsigned 64-bit value (Go ``uint64(int64(n))``)."""
    return n & MASK64


def hash_cell(seed: int, x: int, y: int) -> int:
    """Deterministic 64-bit mix of (seed, x, y) — a SplitMix64-style finalizer.

    Exact port of world.go ``hashCell``. Go's uint64 math wraps mod 2^64, so we
    mask after every multiply/add/shift. Negative coords fold through two's
    complement via :func:`_u64`.
    """
    a = (_u64(seed) * 0x9E3779B97F4A7C15) & MASK64
    b = (_u64(x) * 0xD1B54A32D192ED03) & MASK64
    c = (_u64(y) * 0xF58CCF12EAF4B57B) & MASK64
    z = a ^ b ^ c
    z = (z + 0x9E3779B97F4A7C15) & MASK64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    z = z ^ (z >> 31)
    return z & MASK64


def round_half_away(v: float) -> int:
    """Match Go ``int(math.Round(v))`` — round half away from zero.

    (Python's built-in round() uses banker's rounding; the engine must not.)
    """
    return int(math.floor(v + 0.5)) if v >= 0 else int(math.ceil(v - 0.5))


class Spot:
    __slots__ = ("resource", "remaining", "depleted")

    def __init__(self, resource: str, remaining: int):
        self.resource = resource
        self.remaining = remaining
        self.depleted = False


class Cell:
    __slots__ = ("terrain", "spot", "building")

    def __init__(self, terrain: str = "ground"):
        self.terrain = terrain
        self.spot: Optional[Spot] = None
        self.building: str = ""  # building id, "" if none


class Robot:
    __slots__ = (
        "id", "typ", "pos", "face", "ore", "metal", "cap", "energy",
        "state", "cmd", "queue", "idle_emitted_tick",
    )

    def __init__(self, id, typ, pos, face, cap, energy, state, ore=0, metal=0):
        self.id = id
        self.typ = typ
        self.pos: Tuple[float, float] = pos
        self.face = face
        self.ore = ore
        self.metal = metal
        self.cap = cap
        self.energy = energy
        self.state = state
        self.cmd: Optional[ActiveCmd] = None
        self.queue: List[ActiveCmd] = []
        self.idle_emitted_tick = 0

    def carried(self) -> int:
        return self.ore + self.metal

    def free(self) -> int:
        return self.cap - self.carried()

    def command(self) -> str:
        return self.cmd.cmd if self.cmd is not None else ""

    def cell_f(self) -> Tuple[int, int]:
        return (round_half_away(self.pos[0]), round_half_away(self.pos[1]))


class Construction:
    __slots__ = ("target_type", "req_ore", "req_metal", "got_ore", "got_metal",
                 "progress", "build_ticks")

    def __init__(self, target_type, req_ore, req_metal, build_ticks):
        self.target_type = target_type
        self.req_ore = req_ore
        self.req_metal = req_metal
        self.got_ore = 0
        self.got_metal = 0
        self.progress = 0.0
        self.build_ticks = build_ticks

    def fulfilled(self) -> bool:
        return self.got_ore >= self.req_ore and self.got_metal >= self.req_metal


class Building:
    __slots__ = (
        "id", "typ", "pos", "status", "has_storage", "ore", "metal", "cap",
        "full_emitted", "spot_cell", "prod_queue", "prod_active",
        "prod_progress", "cons",
    )

    def __init__(self, id, typ, pos, status, has_storage=False, cap=0):
        self.id = id
        self.typ = typ
        self.pos: Tuple[int, int] = pos
        self.status = status
        self.has_storage = has_storage
        self.ore = 0
        self.metal = 0
        self.cap = cap
        self.full_emitted = False
        self.spot_cell: Optional[Tuple[int, int]] = None
        self.prod_queue = 0
        self.prod_active = False
        self.prod_progress = 0
        self.cons: Optional[Construction] = None


def ring_offset(i: int) -> Tuple[int, int]:
    offs = [
        (1, 0), (-1, 0), (0, 1), (0, -1),
        (1, 1), (-1, -1), (1, -1), (-1, 1),
        (2, 0), (-2, 0), (0, 2), (0, -2),
    ]
    if i < len(offs):
        return offs[i]
    return (i - len(offs) + 3, 0)


def robot_num(id: str) -> int:
    n = 0
    for ch in id:
        if "0" <= ch <= "9":
            n = n * 10 + (ord(ch) - ord("0"))
    return n


class World:
    """Sparse endless world; materialized on demand (port of world.go)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.city = ""
        self.seed = 0
        self.cells: Dict[Tuple[int, int], Cell] = {}
        self.robots: Dict[str, Robot] = {}
        self.robot_ord: List[str] = []
        self.buildings: Dict[str, Building] = {}
        self.build_ord: List[str] = []
        self.discovered: Dict[Tuple[int, int], bool] = {}
        self.min_x = self.min_y = self.max_x = self.max_y = 0
        self.have_bounds = False
        self.next_robot = 0
        self.next_build = 0
        self.ore_mined = 0
        self.metal_mined = 0
        self.pending_spawn: List[str] = []

    # --- generation -------------------------------------------------------- #
    def generate(self, city: str, seed: int) -> None:
        self.city = city
        self.seed = seed

        base = Building(
            id="base-1", typ=BUILDING_BASE, pos=(0, 0), status="active",
            has_storage=True, cap=self.cfg.base_storage_cap,
        )
        self.cell_at(0, 0).spot = None
        self.add_building(base)

        num = self.cfg.num_start_robots
        if num < 1:
            num = 1
        for i in range(num):
            off = ring_offset(i)
            pos = (float(off[0]), float(off[1]))
            self.next_robot += 1
            r = Robot(
                id="r" + str(self.next_robot), typ="builder", pos=pos, face="S",
                cap=self.cfg.carry_capacity, energy=self.cfg.energy_cap,
                state="idle", ore=self.cfg.start_ore, metal=self.cfg.start_metal,
            )
            self.add_robot(r)
            self.reveal(off[0], off[1], self.cfg.initial_reveal)
            self.pending_spawn.append(r.id)

        self.reveal(0, 0, self.cfg.initial_reveal)

    def cell_at(self, x: int, y: int) -> Cell:
        key = (x, y)
        c = self.cells.get(key)
        if c is not None:
            return c
        c = Cell(terrain="ground")
        h = hash_cell(self.seed, x, y)
        if float(h % 1_000_000) / 1_000_000.0 < self.cfg.spot_density:
            res = "ore"
            if (h >> 21) & 1 == 1:
                res = "metal"
            rich = self.cfg.spot_rich_min
            span = self.cfg.spot_rich_max - self.cfg.spot_rich_min
            if span > 0:
                rich += int((h >> 24) % (span + 1))
            c.spot = Spot(resource=res, remaining=rich)
        self.cells[key] = c
        return c

    # --- membership -------------------------------------------------------- #
    def add_robot(self, r: Robot) -> None:
        self.robots[r.id] = r
        self.robot_ord.append(r.id)
        n = robot_num(r.id)
        if n > self.next_robot:
            self.next_robot = n

    def remove_robot(self, id: str) -> None:
        if id not in self.robots:
            return
        del self.robots[id]
        self.robot_ord.remove(id)

    def add_building(self, b: Building) -> None:
        self.buildings[b.id] = b
        self.build_ord.append(b.id)
        self.cell_at(b.pos[0], b.pos[1]).building = b.id

    def remove_building(self, id: str) -> None:
        b = self.buildings.get(id)
        if b is None:
            return
        cl = self.cell_at(b.pos[0], b.pos[1])
        if cl.building == id:
            cl.building = ""
        del self.buildings[id]
        self.build_ord.remove(id)

    def base(self) -> Optional[Building]:
        for bid in self.build_ord:
            b = self.buildings.get(bid)
            if b is not None and b.typ == BUILDING_BASE:
                return b
        return None

    def building_at(self, x: int, y: int) -> Optional[Building]:
        c = self.cells.get((x, y))
        if c is not None and c.building != "":
            return self.buildings.get(c.building)
        return None

    # --- fog / bounds ------------------------------------------------------ #
    def reveal(self, cx: int, cy: int, r: int) -> None:
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                self.cell_at(x, y)
                self.discovered[(x, y)] = True
                self.grow_bounds(x, y)

    def grow_bounds(self, x: int, y: int) -> None:
        if not self.have_bounds:
            self.min_x = self.max_x = x
            self.min_y = self.max_y = y
            self.have_bounds = True
            return
        if x < self.min_x:
            self.min_x = x
        if x > self.max_x:
            self.max_x = x
        if y < self.min_y:
            self.min_y = y
        if y > self.max_y:
            self.max_y = y

    def free_adjacent(self, x: int, y: int) -> Tuple[int, int]:
        for d in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            nx, ny = x + d[0], y + d[1]
            if self.building_at(nx, ny) is None:
                return (nx, ny)
        return (x, y)
