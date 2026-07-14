"""The live read model, backed by Redis ``city.<id>.state.*``.

On each event the runtime builds a fresh :class:`StateReader` from a one-shot
read of the state store (no local mirror — see communication.md). The handles
below (``RobotHandle``, ``BuildingHandle``, ``World`` …) are thin views over the
parsed dicts. Commands issued through a handle are *recorded* on the active
:class:`~simcode.contract.Accumulator`, not executed.

State store layout (Redis) — each key is a plain JSON **string** (not a hash):

    city.<id>.state.meta       {"tick","seq","city"}
    city.<id>.state.world      {"size":[w,h],"seed"}
    city.<id>.state.robots     JSON ARRAY of {"id","type","pos":[x,y],"facing",
                               "inventory":{"items":{item:qty},"capacity"},"state","command"}
    city.<id>.state.buildings  JSON ARRAY of {"id","type","pos","status","storage",
                               + mining:"spot", base:"production", constructing:"construction"}
    city.<id>.state.tiles      JSON ARRAY of {"x","y","terrain","spot"|null}
    city.<id>.state.stats      JSON object (not needed to drive)
    city.<id>.state.discovered base64 string (exposed raw; not needed to drive)

The runtime GETs (MGETs) and json-parses these; the reader indexes robots /
buildings by id and tiles by "x,y". ``world.tick`` comes from ``state.meta.tick``.

Persistence: the city-wide ``store`` is DURABLE — GAME (engine core) persists it
and the SDK restores it on (re)connect (see ``Runtime.restore_store``), so it
survives a hot-reload / container restart. Per-robot ``memory`` is still
in-process only (live for the process; reset on hot-reload) — see
``_DictWriteProxy``.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from . import _wire as wire
from .contract import Accumulator, make_command


# --------------------------------------------------------------------------- #
# small value views
# --------------------------------------------------------------------------- #
class Store:
    """A multi-item resource pool (#5): item name -> quantity, bounded by a
    shared total ``capacity`` (mixed items share the cap). Backs both a robot's
    ``inventory`` and a building's ``storage``.

    Item access is dict-like and forgiving: ``store["ore"]`` / ``store.get("ore")``
    read 0 for a missing item, and ``"ore" in store`` tests presence.
    """

    __slots__ = ("items", "capacity")

    def __init__(self, data: dict | None):
        data = data or {}
        # Copy so a handle never aliases (and can't mutate) the parsed state.
        self.items: dict[str, int] = dict(data.get("items") or {})
        self.capacity = data.get("capacity", 0)

    @property
    def total(self) -> int:
        """Total item count held (Σ over items)."""
        return sum(self.items.values())

    @property
    def free(self) -> int:
        return max(0, self.capacity - self.total)

    @property
    def is_full(self) -> bool:
        return self.free <= 0

    def get(self, item: str, default: int = 0) -> int:
        return self.items.get(item, default)

    def __getitem__(self, item: str) -> int:
        return self.items.get(item, 0)

    def __contains__(self, item: str) -> bool:
        return item in self.items

    def __iter__(self):
        return iter(self.items)

    def __bool__(self) -> bool:
        return self.total > 0

    def __repr__(self) -> str:
        return f"Store(items={self.items!r}, capacity={self.capacity})"


class _Attr:
    """Generic read-only attribute bag over a dict (spot, production, …)."""

    def __init__(self, data: dict | None):
        self._d = data or {}

    def __getattr__(self, name: str) -> Any:
        d = object.__getattribute__(self, "_d")
        if name in d:
            return d[name]
        # Private/dunder lookups must still raise so Python's own attribute
        # protocols (copy, pickle, repr/IPython helpers, …) behave normally.
        if name.startswith("_"):
            raise AttributeError(name)
        # A missing *public* field reads as None instead of raising, so a
        # defensive `attr.field or default` over an omitempty wire field (e.g.
        # production.queued when 0) never crashes an event handler.
        return None

    def __bool__(self) -> bool:
        return bool(self._d)

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def __repr__(self) -> str:
        return f"_Attr({self._d!r})"


class Cell:
    """A revealed cell — from ``world`` / ``here`` (map revealed by moving)."""

    __slots__ = ("x", "y", "terrain", "_spot", "_building")

    def __init__(self, data: dict):
        self.x = data.get("x")
        self.y = data.get("y")
        self.terrain = data.get("terrain")
        self._spot = data.get("spot")
        self._building = data.get("building")

    @property
    def position(self):
        return (self.x, self.y)

    @property
    def spot(self) -> Optional[_Attr]:
        return _Attr(self._spot) if self._spot else None

    @property
    def building(self):
        return self._building

    def __repr__(self) -> str:
        return f"Cell(pos=({self.x},{self.y}), terrain={self.terrain!r}, spot={self._spot!r})"


# --------------------------------------------------------------------------- #
# in-process write-through dict (store + per-robot memory)
# --------------------------------------------------------------------------- #
class _DictWriteProxy:
    """Dict-like view over a **live** in-process dict (write-through).

    Writes mutate the backing dict in place AND are recorded via ``on_set`` so
    they ride out on the event's intent. For the city-wide ``store`` those writes
    are persisted by GAME and restored on reconnect (durable across hot-reload /
    restart). For per-robot ``r.memory`` the backing dict is in-process only
    (persists across events for the life of the process; resets on hot-reload).
    """

    def __init__(self, backing: dict, on_set):
        self._d = backing            # live reference, mutated in place
        self._on_set = on_set

    def __getitem__(self, key):
        return self._d[key]

    def __setitem__(self, key, value):
        self._d[key] = value
        self._on_set(key, value)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def setdefault(self, key, default=None):
        if key in self._d:
            return self._d[key]
        self[key] = default
        return default

    def __contains__(self, key):
        return key in self._d

    def __iter__(self):
        return iter(self._d)

    def keys(self):
        return self._d.keys()

    def items(self):
        return self._d.items()

    def values(self):
        return self._d.values()

    def __len__(self):
        return len(self._d)

    def to_dict(self) -> dict:
        return dict(self._d)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._d!r})"


# --------------------------------------------------------------------------- #
# robot handle + registry
# --------------------------------------------------------------------------- #
class RobotHandle:
    def __init__(self, robot_id: str, data: dict, reader: "StateReader"):
        self.id = robot_id
        self._d = data or {}
        self._reader = reader
        self._acc: Accumulator = reader.accumulator

    # ----- read state -----
    @property
    def type(self):
        return self._d.get("type")

    @property
    def position(self):
        pos = self._d.get("pos")
        return tuple(pos) if pos is not None else None

    @property
    def facing(self):
        return self._d.get("facing")

    @property
    def state(self):
        return self._d.get("state")

    @property
    def command(self):
        return self._d.get("command")

    @property
    def inventory(self) -> Store:
        return Store(self._d.get("inventory"))

    @property
    def energy(self):
        """Flight battery. Flying spends it; hitting 0 mid-flight destroys the
        robot. Recharge with ``charge()`` while parked on a Flying Station."""
        return self._d.get("energy")

    @property
    def cell(self):
        """The robot's rounded integer cell (where it interacts with buildings)."""
        pos = self.position
        return (round(pos[0]), round(pos[1])) if pos is not None else None

    @property
    def memory(self) -> _DictWriteProxy:
        # In-process per-robot dict (persists across events for the process).
        rid = self.id
        reader = self._reader
        backing = reader.memory_state.setdefault(rid, {})
        return _DictWriteProxy(
            backing,
            lambda _k, _v: reader.accumulator.set_memory(rid, dict(backing)),
        )

    @property
    def here(self) -> "Here":
        return Here(self.position, self._reader)

    def nearest(self, kind: str | None = None, type: str | None = None):
        return self._reader.nearest(self.position, kind=kind, type=type)

    def find(self, cells: Iterable[dict], kind: str | None = None):
        return _find(cells, kind=kind)

    # ----- commands (intents-out) — positional args, engine arg order -----
    def _emit(self, cmd: str, *args) -> "RobotHandle":
        self._acc.add_command(self.id, make_command(cmd, *args))
        return self

    def move_to(self, x, y):
        """Fly in a straight line to (x, y). Flight ignores terrain/occupancy,
        spends energy proportional to distance, and reveals the map en route."""
        return self._emit("move_to", float(x), float(y))

    def charge(self):
        """Recharge the battery while parked on a Flying Station (explicit only;
        holds the robot until full -> charge_complete)."""
        return self._emit("charge")

    def pick_up(self, item=None, amount=None):
        # Multi-item haul (#5). No args -> pick up ALL (fill across items);
        # item only -> all of that item; item + amount -> that amount.
        if item is None:
            return self._emit("pick_up")
        if amount is None:
            return self._emit("pick_up", item)
        return self._emit("pick_up", item, amount)

    def drop(self, item=None, amount=None):
        # No args -> drop ALL held; item only -> all of that item;
        # item + amount -> that amount.
        if item is None:
            return self._emit("drop")
        if amount is None:
            return self._emit("drop", item)
        return self._emit("drop", item, amount)

    def send(self, target_id, payload):
        return self._emit("send", target_id, payload)

    def cancel(self):
        return self._emit("cancel")

    def log(self, msg) -> "RobotHandle":
        self._acc.add_log(self.id, msg)
        return self

    def __repr__(self) -> str:
        return f"RobotHandle(id={self.id!r}, pos={self.position}, state={self.state!r})"


class Here:
    """What is on the robot's current cell (terrain / spot / building)."""

    def __init__(self, position, reader: "StateReader"):
        self._pos = position
        self._reader = reader

    @property
    def _tile(self) -> dict:
        return self._reader.tile_at(self._pos) or {}

    @property
    def terrain(self):
        return self._tile.get("terrain")

    @property
    def spot(self) -> Optional[_Attr]:
        sp = self._tile.get("spot")
        return _Attr(sp) if sp else None

    @property
    def building(self) -> Optional["BuildingHandle"]:
        return self._reader.building_at(self._pos)

    def __repr__(self) -> str:
        return f"Here(pos={self._pos}, terrain={self.terrain!r})"


class RobotRegistry:
    def __init__(self, reader: "StateReader"):
        self._reader = reader

    def __getitem__(self, robot_id: str) -> RobotHandle:
        data = self._reader.robots_raw.get(robot_id, {})
        return RobotHandle(robot_id, data, self._reader)

    def __contains__(self, robot_id: str) -> bool:
        return robot_id in self._reader.robots_raw

    def all(self):
        return [self[rid] for rid in self._reader.robots_raw]

    def of_type(self, t: str):
        return [h for h in self.all() if h.type == t]

    def __iter__(self):
        return iter(self.all())

    def __len__(self):
        return len(self._reader.robots_raw)


# --------------------------------------------------------------------------- #
# building handle + registry
# --------------------------------------------------------------------------- #
class BuildingHandle:
    def __init__(self, building_id: str, data: dict, reader: "StateReader"):
        self.id = building_id
        self._d = data or {}
        self._reader = reader
        self._acc: Accumulator = reader.accumulator

    @property
    def type(self):
        return self._d.get("type")

    @property
    def position(self):
        pos = self._d.get("pos")
        return tuple(pos) if pos is not None else None

    @property
    def status(self):
        return self._d.get("status")

    @property
    def footprint(self):
        """The building's (w, h) cell footprint (min 1×1). ``pos`` is the min
        corner; the building covers every cell in [x, x+w) × [y, y+h) and a
        robot on ANY of them can interact with it."""
        w = self._d.get("w") or 1
        h = self._d.get("h") or 1
        return (w, h)

    # size is an alias for footprint.
    size = footprint

    @property
    def progress(self):
        return self._d.get("progress")

    @property
    def storage(self) -> Store:
        return Store(self._d.get("storage"))

    def stored(self, item: str) -> int:
        """Quantity of ``item`` in this building's storage store (0 if absent)."""
        return self.storage.get(item)

    @property
    def spot(self) -> Optional[_Attr]:
        sp = self._d.get("spot")
        return _Attr(sp) if sp else None

    @property
    def production(self) -> _Attr:
        """Flying Station only: its robot-production status —
        ``.active`` / ``.progress`` / ``.queued``. Empty on other buildings."""
        return _Attr(self._d.get("production"))

    @property
    def construction(self) -> _Attr:
        return _Attr(self._d.get("construction"))

    @property
    def level(self):
        """Base only: its current level (the game objective; starts at 1)."""
        return self._d.get("level")

    @property
    def quest(self) -> Optional[_Attr]:
        """Base only: the current quest — ``.required`` and ``.progress`` are each
        ``{ore, metal}`` (progress = min(stored, required)). None on non-Base
        buildings. Deliver the required raw resources to the Base to level up."""
        q = self._d.get("quest")
        return _Attr(q) if q else None

    # ----- Flying Station commands -----
    def build_robot(self, n: int = 1) -> "BuildingHandle":
        """Flying Station only: queue ``n`` robots built at THIS station. The
        command targets this building's id; the engine rejects a non-station
        target with a ``blocked`` reason ``not_a_station``."""
        self._acc.add_command(self.id, make_command("build_robot", n))
        return self

    def cancel(self) -> "BuildingHandle":
        """Flying Station only: cancel THIS station's production queue."""
        self._acc.add_command(self.id, make_command("base_cancel"))
        return self

    def __repr__(self) -> str:
        return f"BuildingHandle(id={self.id!r}, type={self.type!r}, status={self.status!r})"


class BuildingRegistry:
    def __init__(self, reader: "StateReader"):
        self._reader = reader

    def __getitem__(self, building_id: str) -> BuildingHandle:
        data = self._reader.buildings_raw.get(building_id, {})
        return BuildingHandle(building_id, data, self._reader)

    def __contains__(self, building_id: str) -> bool:
        return building_id in self._reader.buildings_raw

    def all(self):
        return [self[bid] for bid in self._reader.buildings_raw]

    def of_type(self, t: str):
        return [h for h in self.all() if h.type == t]

    def stations(self):
        """All Flying Station handles (alias for ``of_type("flying_station")``).
        A station handle carries ``build_robot`` / ``cancel`` (queue & cancel
        robot production) plus its ``production`` and ``storage``."""
        return self.of_type("flying_station")

    @property
    def base(self) -> Optional[BuildingHandle]:
        for bid, d in self._reader.buildings_raw.items():
            if d.get("type") == "base":
                return BuildingHandle(bid, d, self._reader)
        return None

    def __iter__(self):
        return iter(self.all())

    def __len__(self):
        return len(self._reader.buildings_raw)


# --------------------------------------------------------------------------- #
# world
# --------------------------------------------------------------------------- #
class World:
    def __init__(self, reader: "StateReader"):
        self._reader = reader

    @property
    def tick(self):
        return self._reader.meta_raw.get("tick")

    @property
    def seq(self):
        return self._reader.meta_raw.get("seq")

    @property
    def size(self):
        """Discovered bounding-box extent (w, h). The world is endless — this is
        a viewport hint, not a bound."""
        sz = self._reader.world_raw.get("size")
        return tuple(sz) if sz is not None else None

    @property
    def origin(self):
        """Min (x, y) of the discovered region."""
        o = self._reader.world_raw.get("origin")
        return tuple(o) if o is not None else None

    @property
    def endless(self) -> bool:
        return bool(self._reader.world_raw.get("endless"))

    @property
    def seed(self):
        return self._reader.world_raw.get("seed")

    def build(self, type, x, y):
        """Place a construction site of ``type`` at (x, y) — a world-scoped order,
        not tied to any robot. Robots haul resources to it and it self-completes
        once supplied. type ∈ mining | storage | flying_station."""
        self._reader.accumulator.add_command(
            "world", make_command("build", type, int(x), int(y))
        )
        return self

    @property
    def discovered(self):
        # Engine writes a JSON list of revealed [x, y] cells; exposed raw.
        return self._reader.discovered_raw

    def spots(self):
        out = []
        for tile in self._reader.tiles_raw.values():
            if tile.get("spot"):
                out.append(Cell(tile))
        return out

    def cells(self, region=None):
        cells = [Cell(t) for t in self._reader.tiles_raw.values()]
        if region is None:
            return cells
        (x0, y0), (x1, y1) = region
        return [c for c in cells if x0 <= c.x <= x1 and y0 <= c.y <= y1]


# --------------------------------------------------------------------------- #
# city-wide store proxy
# --------------------------------------------------------------------------- #
class StoreProxy(_DictWriteProxy):
    """City-wide persistent dict; writes flow into the intent's ``store``."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _spot_matches(spot: dict | None, kind: str) -> bool:
    if not spot:
        return False
    if kind in ("spot", "resource_spot"):
        return True
    if kind.endswith("_spot"):
        return spot.get("resource") == kind[: -len("_spot")]
    return spot.get("resource") == kind


def _find(cells: Iterable[dict], kind: str | None = None):
    for raw in cells or []:
        if kind is None:
            return Cell(raw)
        b = raw.get("building")
        if b and (b.get("type") == kind or kind == "building"):
            return Cell(raw)
        if _spot_matches(raw.get("spot"), kind):
            return Cell(raw)
        if raw.get("terrain") == kind:
            return Cell(raw)
    return None


def _manhattan(a, b) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


# --------------------------------------------------------------------------- #
# the reader
# --------------------------------------------------------------------------- #
class StateReader:
    """A one-shot snapshot of ``city.<id>.state.*`` for a single dispatch.

    Built from the parsed JSON strings the engine writes: ``meta``/``world`` are
    objects; ``robots``/``buildings``/``tiles`` are arrays, indexed here by id
    and "x,y". ``store_state`` is the runtime's live store dict (durable —
    restored on connect); ``memory_state`` is in-process only (see module
    docstring).
    """

    def __init__(
        self,
        *,
        meta: dict,
        world: dict,
        robots: list,
        buildings: list,
        tiles: list,
        discovered=None,
        store_state: dict,
        memory_state: dict,
        accumulator: Accumulator,
    ):
        self.meta_raw = meta or {}
        self.world_raw = world or {}
        self.robots_raw = {r["id"]: r for r in (robots or []) if "id" in r}
        self.buildings_raw = {b["id"]: b for b in (buildings or []) if "id" in b}
        self.tiles_raw = {f'{t["x"]},{t["y"]}': t for t in (tiles or []) if "x" in t and "y" in t}
        self.discovered_raw = discovered
        self.store_state = store_state
        self.memory_state = memory_state
        self.accumulator = accumulator

        self.robots = RobotRegistry(self)
        self.buildings = BuildingRegistry(self)
        self.world = World(self)
        self.store = StoreProxy(store_state, accumulator.set_store)

    # ----- spatial lookups (robot positions are floats → round to a cell) -----
    @staticmethod
    def _tile_key(position) -> str:
        return f"{round(position[0])},{round(position[1])}"

    def tile_at(self, position) -> Optional[dict]:
        if position is None:
            return None
        return self.tiles_raw.get(self._tile_key(position))

    def building_at(self, position) -> Optional[BuildingHandle]:
        if position is None:
            return None
        tx, ty = round(position[0]), round(position[1])
        for bid, d in self.buildings_raw.items():
            pos = d.get("pos")
            if pos is None:
                continue
            # pos is the min corner; the building covers its whole w×h footprint,
            # so (tx,ty) hits it if it lies anywhere inside that box.
            w = d.get("w") or 1
            h = d.get("h") or 1
            if pos[0] <= tx < pos[0] + w and pos[1] <= ty < pos[1] + h:
                return BuildingHandle(bid, d, self)
        return None

    def nearest(self, origin, kind: str | None = None, type: str | None = None):
        if origin is None:
            return None
        want_type = type or (kind if kind in wire.BUILDING_TYPES else None)

        best = None
        best_d = None
        # buildings
        if want_type is not None:
            for d in self.buildings_raw.values():
                if d.get("type") != want_type:
                    continue
                pos = d.get("pos")
                if pos is None:
                    continue
                dist = _manhattan(origin, pos)
                if best_d is None or dist < best_d:
                    best_d, best = dist, tuple(pos)
            return best
        # resource spots (kind like "ore_spot")
        if kind is not None:
            for key, tile in self.tiles_raw.items():
                if not _spot_matches(tile.get("spot"), kind):
                    continue
                pos = (tile.get("x"), tile.get("y"))
                dist = _manhattan(origin, pos)
                if best_d is None or dist < best_d:
                    best_d, best = dist, pos
            return best
        return None
