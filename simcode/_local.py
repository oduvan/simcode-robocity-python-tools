"""An OFFLINE, engine-driven runner for the SimCode Python client.

This is the local-test counterpart to :mod:`simcode._runtime`. Instead of talking
to GAME over Redis, it drives the **real** Robot City engine compiled to a
c-shared library (``libengine.so``, built from ``game/enginedl``) directly over an
FFI boundary, one tick at a time — so a user's ``main.py`` runs **unchanged**
against the *actual* game logic (no re-implementation) before they push.

The design mirrors the browser exactly:

* the engine returns a per-tick **delta** (``changes``); the first one is the full
  starting world, later ones are incremental (see ``reducer.ts``);
* we keep a **mirror** of the world as dicts keyed by id / "x,y", updated by
  applying each delta field-wise (the same merge the browser reducer does);
* on each tick we build a fresh :class:`~simcode._state.StateReader` over the
  mirror + a per-tick :class:`~simcode.contract.Accumulator`, then **dispatch**
  every event through the *same* registry / context machinery the live runtime
  uses, and drain the accumulator into intents that become the next tick's
  commands.

Only the transport differs; dispatch, the read model, the handles, and the
command-accumulation path are all the untouched client library code.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import sys
import traceback
from collections import Counter

from ._context import DispatchContext, reset_context, set_context
from ._registry import registry
from ._state import StateReader
from .contract import Accumulator, Event


# --------------------------------------------------------------------------- #
# 1. the c-shared engine over ctypes
# --------------------------------------------------------------------------- #

def _runs_of(cells) -> list:
    """Collapse a set of (x, y) cells into per-row inclusive x-runs [y, x0, x1].

    Mirrors the engine's encoding so the offline runner hands the reader exactly the
    shape the live wire carries.
    """
    by_row: dict[int, list[int]] = {}
    for x, y in cells:
        by_row.setdefault(y, []).append(x)
    out = []
    for y in sorted(by_row):
        xs = sorted(by_row[y])
        start = prev = xs[0]
        for x in xs[1:]:
            if x == prev + 1:
                prev = x
                continue
            out.append([y, start, prev])
            start = prev = x
        out.append([y, start, prev])
    return out


class Engine:
    """Thin ctypes wrapper over ``libengine.so`` (EngineTick / EngineFree).

    ``EngineTick(reqJSON, len) -> char*`` returns a malloc'd, NUL-terminated JSON
    C-string the caller MUST free with ``EngineFree``. We set ``restype`` to
    ``c_void_p`` (NOT ``c_char_p``) so we keep the raw pointer to free it —
    ``c_char_p`` would auto-copy to bytes and lose the pointer, leaking it.
    """

    def __init__(self, so_path: str):
        self.so_path = so_path
        lib = ctypes.CDLL(so_path)
        lib.EngineTick.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.EngineTick.restype = ctypes.c_void_p
        lib.EngineFree.argtypes = [ctypes.c_void_p]
        lib.EngineFree.restype = None
        self._lib = lib

    def tick(self, request: dict) -> dict:
        """JSON-encode ``request``, call EngineTick, copy + free the result, and
        JSON-decode it. Raises on an ``{"error": ...}`` response."""
        raw = json.dumps(request, separators=(",", ":")).encode("utf-8")
        ptr = self._lib.EngineTick(raw, len(raw))
        if not ptr:
            raise RuntimeError("EngineTick returned NULL")
        try:
            out = ctypes.string_at(ptr)  # copy the NUL-terminated JSON out
        finally:
            self._lib.EngineFree(ptr)  # release the FFI-owned buffer
        resp = json.loads(out)
        if isinstance(resp, dict) and "error" in resp:
            raise RuntimeError("engine error: " + str(resp["error"]))
        return resp


# --------------------------------------------------------------------------- #
# 2. the world mirror (delta-applied, browser-parity)
# --------------------------------------------------------------------------- #
class WorldMirror:
    """The full world as dicts, updated by applying each ``changes`` delta.

    Parity with ``reducer.ts``: robots/buildings merge by id **field-wise** on
    their nested objects; the map accumulates (discovered RUNS are unioned in,
    spots are upserted by cell); ``removed`` ids drop out.
    The first delta (full-from-empty) establishes the world; later ones patch it.
    """

    def __init__(self, city: str, seed: int):
        self.city = city
        self.seed = seed
        self.tick = 0
        self.seq = -1
        self.robots: dict[str, dict] = {}
        self.buildings: dict[str, dict] = {}
        self.spots: dict[tuple[int, int], list] = {}  # (x,y) -> [x,y,resource,remaining]
        self.discovered: set[tuple[int, int]] = set()
        self.stats: dict = {}
        # Robots that LEFT the world over the run. A removal alone does not say
        # WHY, and the two reasons are opposites (#73 / forum post 23):
        #   expired   — flew past its lifespan. Inevitable, expected, replace it.
        #   destroyed — battery hit 0 mid-flight. Avoidable; the controller is wrong.
        # The reason only rides on the EVENT, so run_local counts those two and
        # tells us here; `removed` is the raw removal count, used to notice any
        # removal we could not attribute rather than silently mislabelling it.
        self.expired = 0
        self.destroyed = 0
        self.removed = 0
        # durable-ish state surviving across ticks within one local run:
        # the StoreProxy backing dict and per-robot r.memory backing dicts.
        self._store: dict = {}
        self._memory: dict = {}

    def apply(self, delta: dict) -> None:
        if not delta:
            return
        self.tick = delta.get("tick", self.tick)
        if "seq" in delta:
            self.seq = delta["seq"]

        for patch in delta.get("robots") or []:
            rid = patch.get("id")
            if rid is None:
                continue
            self.robots[rid] = _merge_robot(self.robots.get(rid), patch)

        for patch in delta.get("buildings") or []:
            bid = patch.get("id")
            if bid is None:
                continue
            self.buildings[bid] = _merge_building(self.buildings.get(bid), patch)

        # Spots are UPSERTS keyed by cell (a deposit's `remaining` falls as it is
        # mined; 0 means depleted, not gone).
        for sp in delta.get("spots") or []:
            if isinstance(sp, (list, tuple)) and len(sp) == 4:
                self.spots[(int(sp[0]), int(sp[1]))] = list(sp)

        # Discovered arrives as runs to ADD — deltas are incremental, so this is a
        # union, never a replacement.
        for run in delta.get("discovered") or []:
            if isinstance(run, (list, tuple)) and len(run) == 3:
                y, x0, x1 = int(run[0]), int(run[1]), int(run[2])
                for x in range(x0, x1 + 1):
                    self.discovered.add((x, y))

        removed = delta.get("removed") or {}
        for rid in removed.get("robots") or []:
            if self.robots.pop(rid, None) is not None:
                # Count the departure; the REASON comes from the event stream
                # (see the counter note in __init__) — a removal on its own
                # cannot tell end-of-life from an energy death.
                self.removed += 1
        for bid in removed.get("buildings") or []:
            self.buildings.pop(bid, None)

        st = delta.get("stats")
        if st:
            self.stats.update(st)

    # ---- project the mirror into a StateReader the handlers read ----
    def reader(self, accumulator: Accumulator) -> StateReader:
        if self.discovered:
            xs = [c[0] for c in self.discovered]
            ys = [c[1] for c in self.discovered]
            origin = [min(xs), min(ys)]
            size = [max(xs) - min(xs) + 1, max(ys) - min(ys) + 1]
        else:
            origin, size = [0, 0], [0, 0]
        return StateReader(
            meta={"tick": self.tick, "seq": self.seq, "city": self.city},
            world={"seed": self.seed, "size": size, "origin": origin, "endless": True},
            robots=list(self.robots.values()),
            buildings=list(self.buildings.values()),
            spots=[v for _, v in sorted(self.spots.items())],
            discovered=_runs_of(self.discovered),
            store_state=self._store,
            memory_state=self._memory,
            accumulator=accumulator,
        )


def _merge_robot(prev: dict | None, patch: dict) -> dict:
    if prev is None:
        return dict(patch)
    out = {**prev, **patch}
    if patch.get("inventory"):
        out["inventory"] = {**(prev.get("inventory") or {}), **patch["inventory"]}
    return out


def _merge_building(prev: dict | None, patch: dict) -> dict:
    if prev is None:
        return dict(patch)
    out = {**prev, **patch}
    for field in ("storage", "spot", "production", "quest"):
        if patch.get(field):
            out[field] = {**(prev.get(field) or {}), **patch[field]}
    if patch.get("construction"):
        pc = prev.get("construction") or {}
        nc = patch["construction"]
        out["construction"] = {
            "required": {**(pc.get("required") or {}), **(nc.get("required") or {})},
            "delivered": {**(pc.get("delivered") or {}), **(nc.get("delivered") or {})},
            "progress": nc.get("progress", pc.get("progress")),
        }
    return out


# --------------------------------------------------------------------------- #
# 3. the runner
# --------------------------------------------------------------------------- #
def _default_so_path(module: str = "robot-city") -> str:
    """Resolve the engine ``.so`` for ``module`` when the caller gave no explicit path.

    ``$SIMCODE_ENGINE_SO`` (dev override) wins; otherwise download + cache the
    exact engine the server runs for that module (#29) via :mod:`simcode._engine_dl`,
    so local testing "just works" with no manual build.
    """
    from ._engine_dl import ensure_engine

    return ensure_engine(module)


def _import_controller(entry_path: str):
    """Import the user's controller file so its ``@on`` decorators register.

    Each call uses a unique module name so re-running a different controller in the
    same process re-executes its decorators (import caching would otherwise skip
    them). Callers should ``registry.clear()`` first to isolate runs.
    """
    entry_path = os.path.abspath(entry_path)
    name = f"_simcode_local_controller_{abs(hash(entry_path))}_{len(sys.modules)}"
    spec = importlib.util.spec_from_file_location(name, entry_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import controller from {entry_path!r}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    # Let the controller import sibling ``lib/`` modules relative to its own dir.
    entry_dir = os.path.dirname(entry_path)
    added = False
    if entry_dir not in sys.path:
        sys.path.insert(0, entry_dir)
        added = True
    try:
        spec.loader.exec_module(mod)
    finally:
        if added:
            try:
                sys.path.remove(entry_dir)
            except ValueError:
                pass
    return mod


# Robot end-of-life events. The runner always asks the engine for these two (even
# when the controller subscribes to neither) so the summary can report end-of-life
# and energy-death as SEPARATE figures — see the counter note on WorldMirror.
EVENT_ROBOT_EXPIRED = "robot_expired"
EVENT_ROBOT_DESTROYED = "robot_destroyed"
LIFECYCLE_EVENTS = (EVENT_ROBOT_EXPIRED, EVENT_ROBOT_DESTROYED)


def _dispatch_tick(events: list, mirror: WorldMirror, accumulator: Accumulator,
                   err_counter: list, event_counter: Counter,
                   subscribed: set | None = None) -> None:
    """Dispatch every event of one tick through the client library's real machinery.

    One StateReader + one Accumulator for the whole tick (state does not change
    between events of the same tick); a per-event DispatchContext so the module
    proxies (``robots``/``world``/``store``) and ``e`` resolve, and handler errors
    are caught the same way the live ``Runtime.dispatch`` catches them.

    ``subscribed`` is what the CONTROLLER asked for. The runner may ask the engine
    for more than that (the lifecycle events above), so "events seen" still counts
    only what the controller subscribed to — the extra ones are bookkeeping.
    """
    state = mirror.reader(accumulator)
    for env in events:
        ev = Event(env)
        if ev.event == EVENT_ROBOT_EXPIRED:
            mirror.expired += 1
        elif ev.event == EVENT_ROBOT_DESTROYED:
            mirror.destroyed += 1
        if subscribed is None or ev.event in subscribed:
            event_counter[ev.event] += 1
        subs = registry.handlers_for(ev.event)
        if not subs:
            continue
        ctx = DispatchContext(city=mirror.city, state=state,
                              accumulator=accumulator, event=ev)
        token = set_context(ctx)
        try:
            for sub in subs:
                try:
                    sub.handler(ev)
                except Exception:  # one bad handler must not kill the loop
                    err_counter[0] += 1
                    sys.stderr.write(
                        f"[handler error] event={ev.event} robot={ev.robot_id}\n"
                        + traceback.format_exc()
                    )
                finally:
                    registry.fired(ev.event, sub)
        finally:
            reset_context(token)


def _engine_config(city: str, seed: int, city_config: dict | None,
                   module_type: str | None = None) -> dict:
    """The engine request's `config` block. `config` here is the per-city options
    blob, NOT the module's tuning table (`module`) — two different documents that
    both used to be called "config"; see docs/glossary.md."""
    cfg: dict = {"city": city, "seed": seed}
    if city_config:
        cfg["config"] = city_config
    if module_type:
        cfg["type"] = module_type
    return cfg


def run_local(entry_path: str, seed: int = 7, ticks: int = 200,
              so_path: str | None = None, city: str = "local",
              reset_registry: bool = True, module: str = "robot-city",
              city_config: dict | None = None,
              world_source: dict | None = None,
              module_type: str | None = None,
              map_state: dict | None = None,
              initial_store: dict | None = None,
              prime_state: dict | None = None) -> dict:
    """Run a user controller against the real engine for ``ticks`` ticks.

    Imports ``entry_path`` (registering its handlers), then runs the event ->
    intent loop entirely offline. Returns a summary dict. ``module`` selects which
    game module's engine to download when ``so_path`` is None (Elite users pass
    ``module="elite"``).

    ``world_source`` describes WHERE the world came from (which city, which
    server, or an explicit seed). It is copied into the summary's ``world`` block
    so every run — including ``--json`` — states the world it used and a
    substitution cannot pass unnoticed (#73 / forum post 22).

    RESUMING A RUNNING CITY (#73 req 1). Pass all three together:

    * ``map_state``   — the city's saved world envelope ``{tick, seq, world}`` from
      ``GET /api/city/<slug>/save``, handed to the engine as its map state. The
      engine restores it and continues at ``tick + 1``, so robots keep their
      in-flight commands, targets and cargo — the situation every deploy creates.
    * ``initial_store`` — the city's saved store, which lives OUTSIDE the world.
      Resuming the world without it gives a city that looks right and behaves
      wrong: a controller keeping a claim registry or a version stamp there would
      start blank and re-do work the real city has already done.
    * ``prime_state`` — the city's display snapshot, used to seed the READ MODEL.
      This is needed because the engine's delta after a restore is INCREMENTAL
      (it primes its own baseline from the restored world and then reports only
      that tick's changes), so without priming the handlers would see a nearly
      empty world while the engine held the full one. The snapshot is the same
      display projection a live controller reads from ``state.*``, so this is the
      production arrangement — but it is read at the CITY'S CURRENT tick, which
      runs ahead of the last checkpoint, so it can be slightly newer than the
      restored world. The caller is expected to report that skew; see
      ``robocity_sim.cli``.
    """
    if so_path is None:
        so_path = _default_so_path(module)
    if reset_registry:
        registry.clear()

    engine = Engine(so_path)
    mirror = WorldMirror(city, seed)

    _import_controller(entry_path)

    err_counter = [0]
    event_counter: Counter = Counter()
    cmd_counter: Counter = Counter()

    # Resuming: the saved world envelope IS the engine's map state, so the very
    # first call restores instead of generating. None ⇒ a fresh tick-0 world.
    engine_map = map_state
    commands: list = []        # intent envelopes to submit next tick

    # Saved values survive a deploy; in-memory values do not. Seed the store and
    # leave `memory` empty — that is exactly what a real push produces.
    if initial_store:
        mirror._store.update(initial_store)

    # Prime the read model. Only meaningful when resuming (see the docstring):
    # a restored engine reports incremental deltas, so the handlers would
    # otherwise read a nearly empty world on the first ticks.
    if prime_state:
        mirror.apply(prime_state)

    start_tick = int((map_state or {}).get("tick", 0) or 0)
    resumed = map_state is not None
    discovered_start = len(mirror.discovered) if prime_state else None

    for _ in range(ticks):
        subs = registry.events  # picks up runtime subscribe()/@on changes
        subscribed = set(subs)
        # Ask for the two end-of-life events even when the controller ignores
        # them, so the summary can separate "aged out" from "flown flat".
        ask = sorted(subscribed.union(LIFECYCLE_EVENTS))
        resp = engine.tick({
            "config": _engine_config(city, seed, city_config, module_type),
            "subscriptions": ask,
            "map": engine_map,
            "commands": commands,
        })
        mirror.apply(resp.get("changes") or {})
        engine_map = resp.get("new_map")
        if discovered_start is None:
            discovered_start = len(mirror.discovered)

        accumulator = Accumulator()
        _dispatch_tick(resp.get("events") or [], mirror, accumulator,
                       err_counter, event_counter, subscribed)

        # Drain the accumulator into intents (the client library's own path), record command
        # counts, and hand the envelopes back as next tick's commands.
        intents = accumulator.build_intents(city, primary=None)
        commands = [it.to_envelope() for it in intents]
        for it in intents:
            for c in it.commands:
                cmd_counter[c.get("cmd", "?")] += 1
            # Surface r.log(...) lines in the local runner's stdout — live they go to
            # the city feed, but locally they'd otherwise vanish (only print() showed).
            for msg in it.logs:
                print(f"[log {it.robot} t{mirror.tick}] {msg}")

    buildings_by_type: Counter = Counter()
    for b in mirror.buildings.values():
        buildings_by_type[b.get("type", "?")] += 1

    base_level = None
    base_quest = None
    for b in mirror.buildings.values():
        if b.get("type") == "base":
            base_level = b.get("level")
            base_quest = b.get("quest")
            break

    # Every removal should be attributable to one of the two lifecycle events.
    # If one is not, say so rather than folding it into either figure.
    unattributed = max(0, mirror.removed - mirror.expired - mirror.destroyed)

    # On a resumed run the read model was PRIMED from the city's display state,
    # which is read at the city's current tick and so can be newer than the
    # checkpoint the engine restored. The engine's own `stats` are authoritative,
    # so compare them against what the handlers can see and report any gap rather
    # than letting it pass as fact.
    drift = None
    if resumed and mirror.stats:
        eng_r, eng_b = mirror.stats.get("robots"), mirror.stats.get("buildings")
        seen_r, seen_b = len(mirror.robots), len(mirror.buildings)
        if (isinstance(eng_r, int) and eng_r != seen_r) or \
           (isinstance(eng_b, int) and eng_b != seen_b):
            drift = {"engine_robots": eng_r, "read_model_robots": seen_r,
                     "engine_buildings": eng_b, "read_model_buildings": seen_b}

    world = dict(world_source or {})
    world.setdefault("seed", seed)
    world.setdefault("module", module)
    world.setdefault("city", city)
    if city_config:
        world.setdefault("config", city_config)
    world.setdefault("start", f"resumed at tick {start_tick + 1}" if resumed
                     else "fresh world at tick 0")
    world["resumed"] = resumed
    world["start_tick"] = start_tick

    return {
        "world": world,
        "ticks": ticks,
        "resumed": resumed,
        "start_tick": start_tick,
        "first_tick": start_tick + 1 if resumed else 0,
        "read_model_drift": drift,
        "tick": mirror.tick,
        "robots_alive": len(mirror.robots),
        "robots_expired": mirror.expired,
        "robots_destroyed": mirror.destroyed,
        "robots_removed_unattributed": unattributed,
        "buildings": dict(buildings_by_type),
        "base_level": base_level,
        "base_quest": base_quest,
        "handler_errors": err_counter[0],
        "commands": dict(cmd_counter),
        "events": dict(event_counter),
        "discovered_start": discovered_start or 0,
        "discovered_end": len(mirror.discovered),
        "store": dict(mirror._store),
    }


# --------------------------------------------------------------------------- #
# 4. the CLI  (``python -m simcode.local main.py`` / ``simcode-local``)
# --------------------------------------------------------------------------- #
def describe_world(world: dict | None) -> str:
    """One line naming the world a run used and where it came from (#73).

    Printed in the banner AND repeated in the summary, next to the PASS/FAIL line,
    so a run against the wrong world cannot look like a normal run.
    """
    w = world or {}
    origin = w.get("origin") or "unspecified"
    seed = w.get("seed")
    parts = [f"seed {seed}" if seed is not None else "seed ?", origin]
    if w.get("config"):
        parts.append("config: " + ", ".join(sorted(w["config"])))
    if w.get("start"):
        parts.append(str(w["start"]))
    if w.get("store_keys"):
        parts.append(f"store: {w['store_keys']} key(s) restored")
    return " | ".join(parts)


def _format_summary(s: dict) -> str:
    """Render a :func:`run_local` summary as a readable, PASS/FAIL block."""
    def _counts(d: dict) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted(d.items())) or "—"

    errors = s.get("handler_errors", 0)
    # #73 / forum post 23: end-of-life is NOT failure. `expired` = flew past its
    # lifespan (inevitable — build replacements). `destroyed` = battery hit 0
    # mid-flight (avoidable — the controller mis-budgeted energy). Only the second
    # one means something is wrong, so they are reported as separate figures with
    # different wording, and the PASS line keys on `destroyed` alone.
    expired = s.get("robots_expired", 0)
    destroyed = s.get("robots_destroyed", 0)
    unattributed = s.get("robots_removed_unattributed", 0)
    ok = errors == 0
    # A resumed run continues the CITY'S tick numbering, so "5999 / 6000" would be
    # a lie there — show the real range it covered instead (#73 req 1).
    if s.get("resumed"):
        ticks_line = (f"{s.get('first_tick', 0)} -> {s.get('tick', 0)} "
                      f"({s.get('ticks', 0)} ticks, continuing the city's own numbering)")
    else:
        ticks_line = f"{s.get('tick', 0)} / {s.get('ticks', 0)}"
    lines = [
        "LOCAL-RUN SUMMARY",
        f"  world            : {describe_world(s.get('world'))}",
        f"  ticks run        : {ticks_line}",
        f"  robots alive     : {s.get('robots_alive', 0)}",
        f"  robots expired   : {expired}   (end of life — expected; build replacements)",
        f"  robots destroyed : {destroyed}   (out of energy mid-flight — avoidable; check your charging)",
    ]
    if unattributed:
        lines.append(f"  robots lost (unattributed) : {unattributed}")
    lines += [
        f"  buildings        : {_counts(s.get('buildings') or {})}",
        f"  base level       : {s.get('base_level')}",
        f"  handler errors   : {errors}",
        f"  map revealed     : {s.get('discovered_start', 0)} -> {s.get('discovered_end', 0)} cells",
        f"  commands issued  : {_counts(s.get('commands') or {})}",
        f"  events seen      : {_counts(s.get('events') or {})}",
    ]
    drift = s.get("read_model_drift")
    if drift:
        lines.append(
            f"  read model drift : your handlers saw {drift['read_model_robots']} robots / "
            f"{drift['read_model_buildings']} buildings; the engine holds "
            f"{drift['engine_robots']} / {drift['engine_buildings']} "
            f"(the seeded display state was newer than the restored save)")
    if ok and not destroyed:
        lines.append(
            f"LOCAL-CHECK: PASS — 0 handler errors, 0 robots destroyed "
            f"({expired} expired at end of life, which is normal)")
    elif ok:
        lines.append(
            f"LOCAL-CHECK: PASS — 0 handler errors, but {destroyed} robot(s) ran out of "
            f"energy mid-flight (cargo lost); {expired} expired at end of life, which is normal")
    else:
        lines.append(f"LOCAL-CHECK: FAIL — {errors} handler error(s); scroll up for tracebacks")
    lines.append(f"LOCAL-CHECK: world — {describe_world(s.get('world'))}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: run a controller against the real engine and print a summary.

    Usage: ``python -m simcode.local main.py [--ticks N] [--seed S] [--module M] [--json]``.
    Downloads + caches the exact engine the server runs (unless ``$SIMCODE_ENGINE_SO``
    points at a local build). Exit code is 0 on a clean run, 1 if any handler raised,
    2 if the run itself couldn't start (e.g. the engine couldn't be resolved).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m simcode.local",
        description=(
            "Run your SimCode controller against the REAL Robot City engine, offline. "
            "It downloads the exact engine the server runs, drives your main.py against "
            "it for a while, and reports what happened — so you can check a change works "
            "BEFORE you push."
        ),
    )
    parser.add_argument("entry", metavar="main.py", help="path to your controller script")
    parser.add_argument("--ticks", type=int, default=200,
                        help="how many ticks to simulate (default: 200)")
    parser.add_argument("--seed", type=int, default=7,
                        help="world seed (default: 7 — the module's canonical map)")
    parser.add_argument("--module", default="robot-city",
                        help="game module whose engine to run (default: robot-city)")
    parser.add_argument("--json", action="store_true",
                        help="print the raw summary as JSON instead of the readable block")
    args = parser.parse_args(argv)

    if not os.path.exists(args.entry):
        sys.stderr.write(f"no such controller file: {args.entry!r}\n")
        return 2

    try:
        summary = run_local(args.entry, seed=args.seed, ticks=args.ticks,
                            module=args.module,
                            world_source={"origin": f"explicit --seed {args.seed}"})
    except Exception as e:  # engine download/build failure, import error, …
        sys.stderr.write(f"local run could not start: {e}\n")
        return 2

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(_format_summary(summary))
    return 0 if summary.get("handler_errors", 0) == 0 else 1


if __name__ == "__main__":  # `python -m simcode._local` also works
    raise SystemExit(main())
