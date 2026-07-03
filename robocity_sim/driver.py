"""The local simulation driver.

Wires the vendored SDK client (unchanged) to the ported engine in one process:
imports the user's ``main.py`` (registering handlers), then runs the tick loop
that mirrors ``game/core/engine/engine.go`` ``step``. Intents produced by a
dispatch lag one tick before they are applied, exactly as in production.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# The vendored client package (byte-for-byte the production SDK).
from simcode._registry import registry
from simcode._runtime import Runtime

from .config import Config, CANONICAL_SEED, default_config
from .module import Module, Intent, FeedEvent

_STATE_KEYS = ("meta", "world", "robots", "buildings", "tiles", "discovered")
_import_counter = itertools.count()


def _state_key(city: str, name: str) -> str:
    return f"city.{city}.state.{name}"


def load_user_module(path: str):
    """Import the user's controller so its ``@on.*`` decorators register.

    The registry is cleared first and the file imported under a fresh module
    name, so each run starts from a clean slate (module-level globals reset,
    just like a fresh process / hot-reload)."""
    registry.clear()
    abspath = os.path.abspath(path)
    if not os.path.isfile(abspath):
        raise FileNotFoundError(f"controller not found: {abspath}")
    name = f"_simcode_user_main_{next(_import_counter)}"
    spec = importlib.util.spec_from_file_location(name, abspath)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import controller at {abspath}")
    mod = importlib.util.module_from_spec(spec)
    # Ensure `if __name__ == "__main__"` does NOT fire (import, not run).
    spec.loader.exec_module(mod)
    return mod


@dataclass
class SimResult:
    seed: int
    ticks: int
    city: str
    feed: List[tuple] = field(default_factory=list)  # (tick, line)
    summary: Dict = field(default_factory=dict)
    robots_destroyed: int = 0
    errors: List[dict] = field(default_factory=list)  # handler exceptions


class Simulation:
    def __init__(self, city: str = "local", cfg: Optional[Config] = None,
                 seed: int = CANONICAL_SEED):
        self.city = city
        self.cfg = cfg or default_config()
        self.seed = seed
        self.mod = Module(self.cfg)
        self.mod.reset_world(city, seed)
        # Lazily import here so a FakeRedis is only needed at run time.
        from .fakeredis import FakeRedis
        self.redis = FakeRedis()
        self.rt = Runtime(self.redis, city).install()
        self._pending: List[Intent] = []
        self.robots_destroyed = 0
        # Handler exceptions are isolated by the SDK runtime (a bad handler must
        # not kill the loop) exactly as on the server — but for LOCAL debugging we
        # surface them instead of swallowing them silently. The runtime publishes
        # them on the log channel; we drain those here.
        self.errors: List[dict] = []
        self._pub_seen = 0

    def _publish_state(self, tick: int, seq: int) -> None:
        state = self.mod.build_state(tick, seq)
        mapping = {_state_key(self.city, n): json.dumps(state[n]) for n in _STATE_KEYS}
        self.redis.mset_state(mapping)

    def _envelope_to_intent(self, env: dict) -> Intent:
        return Intent(
            robot=env.get("robot", ""),
            commands=env.get("commands", []) or [],
            logs=env.get("logs", []) or [],
        )

    def step(self, tick: int) -> List[FeedEvent]:
        # 1. Apply intents accumulated from the PREVIOUS tick's dispatch.
        submit_events: List[dict] = []
        for intent in self._pending:
            submit_events.extend(self.mod.submit(intent, tick))
        self._pending = []

        # 2. Advance one tick.
        advance_events = self.mod.advance(tick)
        events = submit_events + advance_events

        # 3. Publish authoritative state (so handlers read the current tick).
        self._publish_state(tick, tick)

        # 4. Dispatch each emitted event; collect intents for the next tick.
        new_intents: List[Intent] = []
        for env in events:
            for out in self.rt.dispatch(env):
                new_intents.append(self._envelope_to_intent(out))
        self._pending = new_intents

        # Count destructions for the summary.
        for env in events:
            if env.get("event") == "robot_destroyed":
                self.robots_destroyed += 1

        self._collect_errors()
        return self.mod.drain_feed()

    def _collect_errors(self) -> None:
        """Drain handler-exception records the runtime published on the log
        channel, so a crashing controller is reported rather than silently
        ignored (the whole point of testing locally)."""
        pub = self.redis.published
        while self._pub_seen < len(pub):
            channel, message = pub[self._pub_seen]
            self._pub_seen += 1
            if not str(channel).endswith(".log"):
                continue
            try:
                rec = json.loads(message)
            except Exception:
                continue
            if isinstance(rec, dict) and rec.get("level") == "error":
                self.errors.append(rec)

    def summary(self, tick: int) -> Dict:
        wd = self.mod.wd
        by_type: Dict[str, int] = {}
        for b in wd.buildings.values():
            by_type[b.typ] = by_type.get(b.typ, 0) + 1
        st = self.mod.stats()
        return {
            "final_tick": tick,
            "robots": len(wd.robots),
            "buildings": len(wd.buildings),
            "buildings_by_type": by_type,
            "ore": st["ore"],
            "metal": st["metal"],
            "spots_found": st["spots_found"],
            "discovered_cells": len(wd.discovered),
            "robots_destroyed": self.robots_destroyed,
            "handler_errors": len(self.errors),
        }


def run_simulation(controller_path: str, ticks: int = 500,
                   seed: int = CANONICAL_SEED, city: str = "local",
                   cfg: Optional[Config] = None,
                   on_tick: Optional[Callable[[int, List[FeedEvent]], None]] = None,
                   sim: Optional[Simulation] = None) -> SimResult:
    """Fresh run: import the controller, drive ``ticks`` ticks, collect output.

    ``sim`` lets a caller pass a pre-seeded :class:`Simulation` (used by
    ``--from-live``); otherwise a canonical fresh world is created.
    """
    # Load the controller FIRST (clears + repopulates the global registry), then
    # build the Simulation whose Runtime.install() attaches to that registry.
    # dispatch() resolves handlers straight from the registry, so this order is
    # what makes the freshly-loaded handlers the live ones.
    load_user_module(controller_path)
    if sim is None:
        sim = Simulation(city=city, cfg=cfg, seed=seed)

    result = SimResult(seed=sim.seed, ticks=ticks, city=city)
    for t in range(1, ticks + 1):
        feed = sim.step(t)
        for f in feed:
            result.feed.append((f.tick, f.line()))
        if on_tick is not None:
            on_tick(t, feed)
    result.summary = sim.summary(ticks)
    result.robots_destroyed = sim.robots_destroyed
    result.errors = sim.errors
    return result
