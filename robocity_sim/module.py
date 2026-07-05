"""The Robot City Builder rules engine — a faithful Python port of
``game/modules/robot_city/{module,commands,buildings,events,state}.go``.

One :class:`Module` drives one city. It owns world state, validates+times robot
commands, runs autonomous mining/construction and Base production, and emits the
full event set. Determinism mirrors the Go engine: deterministic iteration order
everywhere (robot_ord / build_ord), pure-hash world generation.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .config import (
    Config, default_config,
    BUILDING_BASE, BUILDING_MINING, BUILDING_STORAGE, BUILDING_FLYING_STATION,
)
from .world import World, Robot, Building, Construction, Spot

# --- event / command name constants (mirror contract/names.go) ------------- #
EVENT_SPAWN = "spawn"
EVENT_TICK = "tick"
EVENT_IDLE = "idle"
EVENT_ARRIVED = "arrived"
EVENT_BLOCKED = "blocked"
EVENT_CONSTRUCTION_STARTED = "construction_started"
EVENT_RESOURCE_DELIVERED = "resource_delivered"
EVENT_CONSTRUCTION_COMPLETE = "construction_complete"
EVENT_SPOT_DEPLETED = "spot_depleted"
EVENT_STORAGE_FULL = "storage_full"
EVENT_INVENTORY_FULL = "inventory_full"
EVENT_ROBOT_PRODUCED = "robot_produced"
EVENT_ROBOT_DESTROYED = "robot_destroyed"
EVENT_CHARGE_COMPLETE = "charge_complete"
EVENT_MESSAGE = "message"
EVENT_BASE_LEVEL_UP = "base_level_up"
EVENT_QUEST_UPDATED = "quest_updated"

CMD_MOVE_TO = "move_to"
CMD_PICK_UP = "pick_up"
CMD_DROP = "drop"
CMD_CHARGE = "charge"
CMD_SEND = "send"
CMD_CANCEL = "cancel"
CMD_BUILD = "build"
CMD_BUILD_ROBOT = "build_robot"
CMD_BASE_CANCEL = "base_cancel"

ALL_COMMANDS = {
    CMD_MOVE_TO, CMD_PICK_UP, CMD_DROP, CMD_CHARGE, CMD_SEND, CMD_CANCEL,
    CMD_BUILD, CMD_BUILD_ROBOT, CMD_BASE_CANCEL,
}

FEED_KIND_LOG = "log"

STATE_MOVING = "moving"
STATE_CHARGING = "charging"
STATE_IDLE = "idle"
STATUS_CONSTRUCTING = "constructing"
STATUS_ACTIVE = "active"


# --- arg helpers (SDK sends already-parsed Python values) ------------------ #
def _arg_float(args, i, default: float) -> float:
    if i < 0 or i >= len(args):
        return default
    v = args[i]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return float(v)


def _arg_int(args, i, default: int) -> int:
    if i < 0 or i >= len(args):
        return default
    v = args[i]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return int(v)  # truncates float, matching Go argInt


def _opt_int(args, i):
    """Return (value, ok). ok is False when absent or not an integer (mirrors
    Go optInt, which json-unmarshals into int and fails on a float)."""
    if i < 0 or i >= len(args):
        return 0, False
    v = args[i]
    if isinstance(v, bool) or not isinstance(v, int):
        return 0, False
    return v, True


def _arg_str(args, i, default: str) -> str:
    if i < 0 or i >= len(args):
        return default
    v = args[i]
    return v if isinstance(v, str) else default


def _max0(a: int) -> int:
    return a if a > 0 else 0


def _face_of(dx: float, dy: float) -> str:
    if abs(dx) >= abs(dy):
        return "E" if dx >= 0 else "W"
    return "S" if dy >= 0 else "N"


def _plat_id(n: int) -> str:
    return "plat-" + str(n)


class FeedEvent:
    __slots__ = ("kind", "robot", "resource", "amount", "text", "tick")

    def __init__(self, kind, robot="", resource="", amount=0, text="", tick=0):
        self.kind = kind
        self.robot = robot
        self.resource = resource
        self.amount = amount
        self.text = text
        self.tick = tick

    def line(self) -> str:
        who = self.robot
        if self.kind == FEED_KIND_LOG:
            if who:
                who += ": "
            return f"t{self.tick} {who}{self.text}"
        if who:
            who += " "
        line = f"t{self.tick} {who}{self.kind}"
        if self.amount != 0:
            line += f" {self.amount}"
        if self.resource:
            line += " " + self.resource
        return line


class Intent:
    """A CODE->GAME intent (already decoded from the SDK envelope)."""

    __slots__ = ("robot", "commands", "logs")

    def __init__(self, robot: str, commands: list, logs: list):
        self.robot = robot
        self.commands = commands  # list of {"cmd","args"}
        self.logs = logs or []


class Module:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or default_config()
        self.wd = World(self.cfg)
        self.evbuf: List[dict] = []
        self.feed: List[FeedEvent] = []
        self.tick = 0
        # Latches the one-time initial quest_updated emission per (re)start.
        self.quest_announced = False

    # --- lifecycle --------------------------------------------------------- #
    def reset_world(self, city: str, seed: int) -> None:
        self.wd = World(self.cfg)
        self.wd.generate(city, seed)
        self.feed = []
        self.quest_announced = False

    # --- emit / feed ------------------------------------------------------- #
    def emit(self, name: str, robot: str, tick: int, payload: Optional[dict]) -> None:
        env = {
            "type": "event",
            "event": name,
            "robot": robot,
            "tick": tick,
        }
        if payload is not None:
            env["payload"] = payload
        self.evbuf.append(env)

    def feed_add(self, f: FeedEvent) -> None:
        if f.tick == 0:
            f.tick = self.tick
        self.feed.append(f)

    def drain_feed(self) -> List[FeedEvent]:
        out = self.feed
        self.feed = []
        return out

    # --- Submit (intents in) ----------------------------------------------- #
    def submit(self, intent: Intent, tick: int) -> List[dict]:
        self.evbuf = []
        self.tick = tick
        wd = self.wd

        for line in intent.logs:
            self.feed_add(FeedEvent(kind=FEED_KIND_LOG, robot=intent.robot, text=line))

        robot_cmds = []
        for c in intent.commands:
            cmd = c.get("cmd")
            args = c.get("args", [])
            if cmd == CMD_BUILD_ROBOT:
                b = wd.base()
                if b is not None:
                    n = _arg_int(args, 0, 1)
                    if n < 1:
                        n = 1
                    b.prod_queue += n
            elif cmd == CMD_BASE_CANCEL:
                b = wd.base()
                if b is not None:
                    b.prod_queue = 0
            elif cmd == CMD_BUILD:
                self._do_build(args, tick)
            else:
                robot_cmds.append(c)

        if not robot_cmds:
            return self.evbuf
        r = wd.robots.get(intent.robot)
        if r is None:
            return self.evbuf

        if r.cmd is not None:
            self.emit(EVENT_BLOCKED, r.id, tick, {"reason": "interrupted"})
            self.feed_add(FeedEvent(kind=EVENT_BLOCKED, robot=r.id))
        r.cmd = None
        r.queue = []
        r.idle_emitted_tick = 0

        cmds = []
        for c in robot_cmds:
            if c.get("cmd") not in ALL_COMMANDS:
                continue
            cmds.append(_ActiveCmd(c.get("cmd"), c.get("args", [])))
        if not cmds:
            r.state = STATE_IDLE
            return self.evbuf
        r.cmd = cmds[0]
        r.queue = cmds[1:]
        self._activate(r, tick)
        return self.evbuf

    def _activate(self, r: Robot, tick: int) -> None:
        while r.cmd is not None:
            if not self._begin_cmd(r, tick):
                return
            self._pop_cmd(r)
        r.state = STATE_IDLE

    def _pop_cmd(self, r: Robot) -> None:
        r.cmd = None
        if r.queue:
            r.cmd = r.queue[0]
            r.queue = r.queue[1:]

    def _finish_cmd(self, r: Robot, tick: int) -> None:
        self._pop_cmd(r)
        if r.cmd is not None:
            self._activate(r, tick)
        else:
            r.state = STATE_IDLE

    # --- begin / advance commands ------------------------------------------ #
    def _begin_cmd(self, r: Robot, tick: int) -> bool:
        cmd = r.cmd.cmd
        if cmd == CMD_MOVE_TO:
            x = _arg_float(r.cmd.args, 0, r.pos[0])
            y = _arg_float(r.cmd.args, 1, r.pos[1])
            r.cmd.target = (x, y)
            r.state = STATE_MOVING
            return False
        if cmd == CMD_CHARGE:
            if self._station_at(r.cell_f()) is None:
                self._blocked(r, tick, "no_station")
                return True
            if r.energy >= self.cfg.energy_cap:
                self.emit(EVENT_CHARGE_COMPLETE, r.id, tick, {"energy": r.energy})
                self.feed_add(FeedEvent(kind=EVENT_CHARGE_COMPLETE, robot=r.id))
                r.state = STATE_IDLE
                return True
            r.state = STATE_CHARGING
            return False
        if cmd == CMD_DROP:
            self._do_drop(r, tick)
            return True
        if cmd == CMD_PICK_UP:
            self._do_pick_up(r, tick)
            return True
        if cmd == CMD_SEND:
            self._do_send(r, tick)
            return True
        if cmd == CMD_CANCEL:
            return True
        return True

    def _advance_robot(self, r: Robot, tick: int) -> None:
        if r.cmd is None:
            return
        if r.cmd.cmd == CMD_MOVE_TO:
            self._advance_move(r, tick)
        elif r.cmd.cmd == CMD_CHARGE:
            self._advance_charge(r, tick)

    def _blocked(self, r: Robot, tick: int, reason: str) -> None:
        self.emit(EVENT_BLOCKED, r.id, tick, {"reason": reason})
        self.feed_add(FeedEvent(kind=EVENT_BLOCKED, robot=r.id))
        r.state = STATE_IDLE

    def _station_at(self, c) -> Optional[Building]:
        b = self.wd.building_at(c[0], c[1])
        if b is not None and b.status == STATUS_ACTIVE and \
                b.typ in (BUILDING_FLYING_STATION, BUILDING_BASE):
            return b
        return None

    def _advance_move(self, r: Robot, tick: int) -> None:
        wd = self.wd
        dx = r.cmd.target[0] - r.pos[0]
        dy = r.cmd.target[1] - r.pos[1]
        dist = math.hypot(dx, dy)
        if dist < 1e-9:
            self._arrive_move(r, tick)
            return
        r.face = _face_of(dx, dy)

        move = self.cfg.fly_speed
        if move > dist:
            move = dist
        cost = move * self.cfg.energy_per_distance

        if cost > r.energy:
            reach = 0.0
            if self.cfg.energy_per_distance > 0:
                reach = r.energy / self.cfg.energy_per_distance
            frac = reach / dist
            r.pos = (r.pos[0] + dx * frac, r.pos[1] + dy * frac)
            r.energy = 0
            cf = r.cell_f()
            wd.reveal(cf[0], cf[1], self.cfg.move_reveal)
            self._destroy_robot(r, tick, "out_of_energy")
            return

        r.energy -= cost
        frac = move / dist
        r.pos = (r.pos[0] + dx * frac, r.pos[1] + dy * frac)
        cf = r.cell_f()
        wd.reveal(cf[0], cf[1], self.cfg.move_reveal)

        if move >= dist - 1e-9:
            r.pos = r.cmd.target
            self._arrive_move(r, tick)

    def _arrive_move(self, r: Robot, tick: int) -> None:
        self.emit(EVENT_ARRIVED, r.id, tick, {"position": [r.pos[0], r.pos[1]]})
        self._finish_cmd(r, tick)

    def _destroy_robot(self, r: Robot, tick: int, reason: str) -> None:
        self.emit(EVENT_ROBOT_DESTROYED, r.id, tick,
                  {"position": [r.pos[0], r.pos[1]], "reason": reason})
        self.feed_add(FeedEvent(kind=EVENT_ROBOT_DESTROYED, robot=r.id))
        self.wd.remove_robot(r.id)

    def _advance_charge(self, r: Robot, tick: int) -> None:
        if self._station_at(r.cell_f()) is None:
            self._blocked(r, tick, "left_station")
            self._finish_cmd(r, tick)
            return
        r.energy += self.cfg.charge_rate
        if r.energy >= self.cfg.energy_cap:
            r.energy = self.cfg.energy_cap
            self.emit(EVENT_CHARGE_COMPLETE, r.id, tick, {"energy": r.energy})
            self.feed_add(FeedEvent(kind=EVENT_CHARGE_COMPLETE, robot=r.id))
            self._finish_cmd(r, tick)

    # --- world.build ------------------------------------------------------- #
    def _do_build(self, args, tick: int) -> None:
        wd = self.wd
        typ = _arg_str(args, 0, "")
        x = _arg_int(args, 1, 0)
        y = _arg_int(args, 2, 0)

        # (x,y) is the anchor = min corner; the building occupies the whole w x h box.
        w, h = self.cfg.footprint(typ)
        reason = ""
        if typ == BUILDING_BASE:
            reason = "base_not_buildable"
        elif typ not in self.cfg.recipes:
            reason = "unknown_type"
        elif not wd.footprint_free(x, y, w, h):
            reason = "cell_occupied"
        if reason == "" and typ == BUILDING_MINING:
            cl = wd.cell_at(x, y)
            if cl.spot is None or cl.spot.remaining <= 0:
                reason = "no_spot"
        if reason != "":
            self.emit(EVENT_BLOCKED, "", tick, {"reason": reason})
            self.feed_add(FeedEvent(kind=EVENT_BLOCKED))
            return

        recipe = self.cfg.recipes[typ]
        wd.next_build += 1
        bid = _plat_id(wd.next_build)
        b = Building(id=bid, typ=typ, pos=(x, y), status=STATUS_CONSTRUCTING)
        b.cons = Construction(target_type=typ, req_ore=recipe.ore,
                              req_metal=recipe.metal, build_ticks=recipe.build_ticks)
        wd.add_building(b)
        wd.reveal(x, y, self.cfg.move_reveal)
        self.emit(EVENT_CONSTRUCTION_STARTED, "", tick,
                  {"building_id": bid, "type": typ})
        self.feed_add(FeedEvent(kind=EVENT_CONSTRUCTION_STARTED))

    def _deposit_target(self, r: Robot) -> Optional[Building]:
        wd = self.wd
        c = r.cell_f()
        b = wd.building_at(c[0], c[1])
        if b is not None:
            return b
        for d in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            bb = wd.building_at(c[0] + d[0], c[1] + d[1])
            if bb is not None and bb.has_storage:
                return bb
        return None

    def _do_drop(self, r: Robot, tick: int) -> None:
        b = self._deposit_target(r)
        if b is None:
            self._blocked(r, tick, "nothing_here")
            return
        ore, ok_o = _opt_int(r.cmd.args, 0)
        metal, ok_m = _opt_int(r.cmd.args, 1)
        if not ok_o:
            ore = r.ore
        if not ok_m:
            metal = r.metal
        ore = min(_max0(ore), r.ore)
        metal = min(_max0(metal), r.metal)

        if b.status == STATUS_CONSTRUCTING and b.cons is not None:
            take_ore = _max0(min(ore, b.cons.req_ore - b.cons.got_ore))
            take_metal = _max0(min(metal, b.cons.req_metal - b.cons.got_metal))
            b.cons.got_ore += take_ore
            b.cons.got_metal += take_metal
            r.ore -= take_ore
            r.metal -= take_metal
            self.emit(EVENT_RESOURCE_DELIVERED, r.id, tick,
                      {"building_id": b.id, "ore": take_ore, "metal": take_metal})
            self.feed_add(FeedEvent(kind=EVENT_RESOURCE_DELIVERED, robot=r.id))
            r.state = STATE_IDLE
            return

        if not b.has_storage:
            self._blocked(r, tick, "no_storage")
            return
        room = b.cap - (b.ore + b.metal)
        take_ore = min(ore, room)
        room -= take_ore
        take_metal = min(metal, room)
        b.ore += take_ore
        b.metal += take_metal
        r.ore -= take_ore
        r.metal -= take_metal
        self.emit(EVENT_RESOURCE_DELIVERED, r.id, tick,
                  {"building_id": b.id, "ore": take_ore, "metal": take_metal})
        self.feed_add(FeedEvent(kind=EVENT_RESOURCE_DELIVERED, robot=r.id))
        if b.ore + b.metal >= b.cap:
            self.emit(EVENT_STORAGE_FULL, r.id, tick, {"building_id": b.id})
            self.feed_add(FeedEvent(kind=EVENT_STORAGE_FULL, robot=r.id))
        r.state = STATE_IDLE

    def _do_pick_up(self, r: Robot, tick: int) -> None:
        c = r.cell_f()
        b = self.wd.building_at(c[0], c[1])
        if b is None:
            self._blocked(r, tick, "nothing_here")
            return
        if b.typ == BUILDING_BASE:
            self._blocked(r, tick, "base_reserved")
            return
        if not b.has_storage:
            self._blocked(r, tick, "no_storage")
            return
        ore, ok_o = _opt_int(r.cmd.args, 0)
        metal, ok_m = _opt_int(r.cmd.args, 1)
        if not ok_o:
            ore = b.ore
        if not ok_m:
            metal = b.metal
        ore = min(_max0(ore), b.ore)
        metal = min(_max0(metal), b.metal)
        take_ore = min(ore, r.free())
        take_metal = min(metal, r.free() - take_ore)
        b.ore -= take_ore
        b.metal -= take_metal
        r.ore += take_ore
        r.metal += take_metal
        if b.full_emitted and b.ore + b.metal < b.cap:
            b.full_emitted = False
        if r.free() == 0:
            self.emit(EVENT_INVENTORY_FULL, r.id, tick, None)
            self.feed_add(FeedEvent(kind=EVENT_INVENTORY_FULL, robot=r.id))
        r.state = STATE_IDLE

    def _do_send(self, r: Robot, tick: int) -> None:
        target = _arg_str(r.cmd.args, 0, "")
        if target not in self.wd.robots:
            self._blocked(r, tick, "no_target")
            return
        payload_val = r.cmd.args[1] if len(r.cmd.args) > 1 else None
        self.emit(EVENT_MESSAGE, target, tick, {"from": r.id, "payload": payload_val})
        self.feed_add(FeedEvent(kind=EVENT_MESSAGE, robot=r.id))
        r.state = STATE_IDLE

    # --- Advance (one tick) ------------------------------------------------ #
    def advance(self, tick: int) -> List[dict]:
        self.evbuf = []
        self.tick = tick
        wd = self.wd

        self.emit(EVENT_TICK, "", tick, {"tick_no": tick})

        for rid in wd.pending_spawn:
            self.emit(EVENT_SPAWN, rid, tick, None)
        wd.pending_spawn = []

        self._advance_production(tick)
        # Base quests / leveling (the objective): consume-and-level-up when the
        # store satisfies the current quest. Runs after production so both compete
        # for the same reserved store.
        self._advance_base_quest(tick)
        self._advance_mining(tick)

        for rid in list(wd.robot_ord):
            r = wd.robots.get(rid)
            if r is not None:
                self._advance_robot(r, tick)

        self._advance_constructions(tick)
        self._notify_idle(tick)
        return self.evbuf

    def _notify_idle(self, tick: int) -> None:
        resend = self.cfg.idle_resend_ticks
        for rid in self.wd.robot_ord:
            r = self.wd.robots[rid]
            if r.cmd is not None or r.queue:
                continue
            if r.idle_emitted_tick == 0 or (resend > 0 and tick - r.idle_emitted_tick >= resend):
                r.idle_emitted_tick = tick
                self.emit(EVENT_IDLE, r.id, tick, None)

    def _advance_production(self, tick: int) -> None:
        wd = self.wd
        b = wd.base()
        if b is None:
            return
        rr = self.cfg.robot_recipe
        if not b.prod_active and b.prod_queue > 0:
            if b.ore >= rr.ore and b.metal >= rr.metal:
                b.ore -= rr.ore
                b.metal -= rr.metal
                b.prod_active = True
                b.prod_progress = 0
        if b.prod_active:
            b.prod_progress += 1
            if b.prod_progress >= rr.build_ticks:
                pos = wd.free_adjacent(b.pos[0], b.pos[1])
                wd.next_robot += 1
                rid = "r" + str(wd.next_robot)
                nr = Robot(
                    id=rid, typ="builder", pos=(float(pos[0]), float(pos[1])),
                    face="S", cap=self.cfg.carry_capacity, energy=self.cfg.energy_cap,
                    state=STATE_IDLE, ore=self.cfg.produced_ore, metal=self.cfg.produced_metal,
                )
                wd.add_robot(nr)
                wd.reveal(pos[0], pos[1], self.cfg.initial_reveal)
                b.prod_active = False
                b.prod_progress = 0
                if b.prod_queue > 0:
                    b.prod_queue -= 1
                self.emit(EVENT_ROBOT_PRODUCED, rid, tick, {"robot_id": rid})
                self.feed_add(FeedEvent(kind=EVENT_ROBOT_PRODUCED, robot=rid))
                self.emit(EVENT_SPAWN, rid, tick, None)

    def _advance_base_quest(self, tick: int) -> None:
        """The Base's leveling (the game objective). Announce the current quest
        once, then — while the Base store satisfies the current quest — CONSUME
        the required ore+metal and level up, emitting base_level_up +
        quest_updated. The same store also pays robot production, so quest goods
        and robots compete for it. (Mirror of buildings.go advanceBaseQuest.)"""
        b = self.wd.base()
        if b is None:
            return
        if b.level < 1:
            b.level = 1
        if not self.quest_announced:
            self.quest_announced = True
            req_ore, req_metal = self.cfg.quest_for(b.level)
            self.emit(EVENT_QUEST_UPDATED, b.id, tick,
                      {"level": b.level, "requirements": {"ore": req_ore, "metal": req_metal}})
            self.feed_add(FeedEvent(kind=EVENT_QUEST_UPDATED))
        # Level up while the store can pay the current quest (loop so a big surplus
        # can clear multiple levels in one tick; the requirement grows each level).
        while True:
            req_ore, req_metal = self.cfg.quest_for(b.level)
            if b.ore < req_ore or b.metal < req_metal:
                break
            b.ore -= req_ore
            b.metal -= req_metal
            b.level += 1
            next_ore, next_metal = self.cfg.quest_for(b.level)
            self.emit(EVENT_BASE_LEVEL_UP, b.id, tick,
                      {"level": b.level, "quest": {"ore": next_ore, "metal": next_metal}})
            self.feed_add(FeedEvent(kind=EVENT_BASE_LEVEL_UP, amount=b.level))
            self.emit(EVENT_QUEST_UPDATED, b.id, tick,
                      {"level": b.level, "requirements": {"ore": next_ore, "metal": next_metal}})
            self.feed_add(FeedEvent(kind=EVENT_QUEST_UPDATED))

    def objective(self) -> str:
        """The game-agnostic one-line goal summary (Base level + next quest).
        Empty when there is no Base. (Mirror of module.go objective.)"""
        b = self.wd.base()
        if b is None:
            return ""
        lvl = b.level if b.level >= 1 else 1
        ore, metal = self.cfg.quest_for(lvl)
        return f"⭐ Base level {lvl} — next: {ore} ore + {metal} metal"

    def _advance_mining(self, tick: int) -> None:
        wd = self.wd
        for bid in wd.build_ord:
            b = wd.buildings.get(bid)
            if b is None or b.typ != BUILDING_MINING or b.status != STATUS_ACTIVE:
                continue
            if b.spot_cell is None:
                continue
            cl = wd.cell_at(b.spot_cell[0], b.spot_cell[1])
            if cl.spot is None or cl.spot.remaining <= 0:
                continue
            room = b.cap - (b.ore + b.metal)
            if room <= 0:
                if not b.full_emitted:
                    b.full_emitted = True
                    self.emit(EVENT_STORAGE_FULL, "", tick, {"building_id": b.id})
                    self.feed_add(FeedEvent(kind=EVENT_STORAGE_FULL))
                continue
            amount = min(self.cfg.mining_speed, min(cl.spot.remaining, room))
            cl.spot.remaining -= amount
            if cl.spot.resource == "ore":
                b.ore += amount
                wd.ore_mined += amount
            else:
                b.metal += amount
                wd.metal_mined += amount
            if cl.spot.remaining <= 0:
                cl.spot.depleted = True
                self.emit(EVENT_SPOT_DEPLETED, "", tick, {"building_id": b.id})
                self.feed_add(FeedEvent(kind=EVENT_SPOT_DEPLETED))

    def _advance_constructions(self, tick: int) -> None:
        wd = self.wd
        ids = list(wd.build_ord)
        for bid in ids:
            b = wd.buildings.get(bid)
            if b is None or b.status != STATUS_CONSTRUCTING or b.cons is None:
                continue
            if not b.cons.fulfilled():
                continue
            bt = b.cons.build_ticks
            if bt < 1:
                bt = 1
            b.cons.progress += 1.0 / float(bt)
            if b.cons.progress >= 1.0:
                self._complete_construction(b, tick)

    def _complete_construction(self, plat: Building, tick: int) -> None:
        wd = self.wd
        typ = plat.cons.target_type
        pos = plat.pos
        wd.next_build += 1
        new_id = typ + "-" + str(wd.next_build)
        nb = Building(id=new_id, typ=typ, pos=pos, status=STATUS_ACTIVE)
        if typ == BUILDING_MINING:
            nb.has_storage = True
            nb.cap = self.cfg.mining_storage_cap
            nb.spot_cell = (pos[0], pos[1])
        elif typ == BUILDING_STORAGE:
            nb.has_storage = True
            nb.cap = self.cfg.storage_cap
        wd.remove_building(plat.id)
        wd.add_building(nb)
        self.emit(EVENT_CONSTRUCTION_COMPLETE, "", tick,
                  {"building_id": new_id, "type": typ})
        self.feed_add(FeedEvent(kind=EVENT_CONSTRUCTION_COMPLETE))

    # --- state snapshot (state.* dicts the SDK reads) ---------------------- #
    def _robot_form(self, r: Robot) -> dict:
        return {
            "id": r.id, "type": r.typ, "pos": [r.pos[0], r.pos[1]],
            "facing": r.face,
            "inventory": {"ore": r.ore, "metal": r.metal, "capacity": r.cap},
            "energy": r.energy, "state": r.state, "command": r.command(),
        }

    def _building_form(self, b: Building) -> dict:
        bf: dict = {"id": b.id, "type": b.typ, "pos": [b.pos[0], b.pos[1]],
                    "w": b.w if b.w >= 1 else 1, "h": b.h if b.h >= 1 else 1,
                    "status": b.status}
        if b.has_storage:
            bf["storage"] = {"ore": b.ore, "metal": b.metal, "capacity": b.cap}
        if b.typ == BUILDING_MINING:
            cl = self.wd.cell_at(b.pos[0], b.pos[1])
            if cl.spot is not None:
                bf["spot"] = {"resource": cl.spot.resource, "remaining": cl.spot.remaining}
        if b.typ == BUILDING_BASE:
            denom = self.cfg.robot_recipe.build_ticks or 1
            bf["production"] = {
                "active": b.prod_active,
                "progress": float(b.prod_progress) / float(denom),
                "queued": b.prod_queue,
            }
            # Leveling: the Base's current level + quest (required vs progress).
            # This is the game objective; only the Base carries it.
            lvl = b.level if b.level >= 1 else 1
            req_ore, req_metal = self.cfg.quest_for(lvl)
            bf["level"] = lvl
            bf["quest"] = {
                "required": {"ore": req_ore, "metal": req_metal},
                "progress": {"ore": min(b.ore, req_ore), "metal": min(b.metal, req_metal)},
            }
        if b.status == STATUS_CONSTRUCTING and b.cons is not None:
            bf["construction"] = {
                "required": {"ore": b.cons.req_ore, "metal": b.cons.req_metal},
                "delivered": {"ore": b.cons.got_ore, "metal": b.cons.got_metal},
                "progress": b.cons.progress,
            }
        return bf

    def _tile_form(self, x: int, y: int) -> dict:
        cl = self.wd.cell_at(x, y)
        t = {"x": x, "y": y, "terrain": cl.terrain, "spot": None}
        if cl.spot is not None:
            t["spot"] = {"resource": cl.spot.resource, "remaining": cl.spot.remaining}
        return t

    def stats(self) -> dict:
        wd = self.wd
        ore_stored = metal_stored = spots = 0
        for b in wd.buildings.values():
            ore_stored += b.ore
            metal_stored += b.metal
        for r in wd.robots.values():
            ore_stored += r.ore
            metal_stored += r.metal
        for c in wd.discovered:
            if wd.cell_at(c[0], c[1]).spot is not None:
                spots += 1
        return {
            "robots": len(wd.robots),
            "buildings": len(wd.buildings),
            "ore": {"mined": wd.ore_mined, "stored": ore_stored},
            "metal": {"mined": wd.metal_mined, "stored": metal_stored},
            "spots_found": spots,
        }

    def world_header(self) -> dict:
        wd = self.wd
        w = {"seed": wd.seed, "endless": True, "size": [0, 0], "origin": [0, 0]}
        if wd.have_bounds:
            w["origin"] = [wd.min_x, wd.min_y]
            w["size"] = [wd.max_x - wd.min_x + 1, wd.max_y - wd.min_y + 1]
        return w

    def sorted_cells(self):
        return sorted(self.wd.discovered.keys(), key=lambda c: (c[1], c[0]))

    def build_state(self, tick: int, seq: int) -> Dict[str, Any]:
        """Produce the state.* dicts the SDK StateReader consumes."""
        wd = self.wd
        cells = self.sorted_cells()
        tiles = [self._tile_form(c[0], c[1]) for c in cells]
        robots = [self._robot_form(wd.robots[i]) for i in sorted(wd.robots.keys())]
        buildings = [self._building_form(wd.buildings[i]) for i in sorted(wd.buildings.keys())]
        return {
            "meta": {"tick": tick, "seq": seq, "city": wd.city},
            "world": self.world_header(),
            "robots": robots,
            "buildings": buildings,
            "tiles": tiles,
            "discovered": [[c[0], c[1]] for c in cells],
            "stats": self.stats(),
            # Game-agnostic goal summary the shell renders in the topbar.
            "objective": self.objective(),
        }


class _ActiveCmd:
    """Wraps a submitted command (cmd + positional args)."""

    __slots__ = ("cmd", "args", "target")

    def __init__(self, cmd: str, args: list):
        self.cmd = cmd
        self.args = args or []
        self.target = (0.0, 0.0)
