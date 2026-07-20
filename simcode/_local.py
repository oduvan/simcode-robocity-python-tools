"""An OFFLINE, engine-driven runner for the SimCode Python SDK.

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
command-accumulation path are all the untouched SDK code.
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
    their nested objects; tiles/discovered accumulate; ``removed`` ids drop out.
    The first delta (full-from-empty) establishes the world; later ones patch it.
    """

    def __init__(self, city: str, seed: int):
        self.city = city
        self.seed = seed
        self.tick = 0
        self.seq = -1
        self.robots: dict[str, dict] = {}
        self.buildings: dict[str, dict] = {}
        self.tiles: dict[str, dict] = {}  # "x,y" -> tile
        self.discovered: set[tuple[int, int]] = set()
        self.stats: dict = {}
        # counters observed over the whole run
        self.destroyed = 0
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

        for t in delta.get("tiles") or []:
            if "x" in t and "y" in t:
                self.tiles[f'{t["x"]},{t["y"]}'] = t
                self.discovered.add((t["x"], t["y"]))

        for xy in delta.get("discovered") or []:
            self.discovered.add((xy[0], xy[1]))

        removed = delta.get("removed") or {}
        for rid in removed.get("robots") or []:
            if self.robots.pop(rid, None) is not None:
                # In this module a robot only ever leaves the world by being
                # destroyed (out of energy mid-flight), so a removed robot id is a
                # faithful destroyed-count signal, independent of subscriptions.
                self.destroyed += 1
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
            tiles=list(self.tiles.values()),
            discovered=[list(c) for c in self.discovered],
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


def _dispatch_tick(events: list, mirror: WorldMirror, accumulator: Accumulator,
                   err_counter: list, event_counter: Counter) -> None:
    """Dispatch every event of one tick through the SDK's real machinery.

    One StateReader + one Accumulator for the whole tick (state does not change
    between events of the same tick); a per-event DispatchContext so the module
    proxies (``robots``/``world``/``store``) and ``e`` resolve, and handler errors
    are caught the same way the live ``Runtime.dispatch`` catches them.
    """
    state = mirror.reader(accumulator)
    for env in events:
        ev = Event(env)
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


def run_local(entry_path: str, seed: int = 7, ticks: int = 200,
              so_path: str | None = None, city: str = "local",
              reset_registry: bool = True, module: str = "robot-city") -> dict:
    """Run a user controller against the real engine for ``ticks`` ticks.

    Imports ``entry_path`` (registering its handlers), then runs the event ->
    intent loop entirely offline. Returns a summary dict. ``module`` selects which
    game module's engine to download when ``so_path`` is None (Elite users pass
    ``module="elite"``).
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

    engine_map = None          # opaque new_map envelope; None on the first call
    commands: list = []        # intent envelopes to submit next tick
    discovered_start = None

    for _ in range(ticks):
        subs = registry.events  # picks up runtime subscribe()/@on changes
        resp = engine.tick({
            "config": {"city": city, "seed": seed},
            "subscriptions": subs,
            "map": engine_map,
            "commands": commands,
        })
        mirror.apply(resp.get("changes") or {})
        engine_map = resp.get("new_map")
        if discovered_start is None:
            discovered_start = len(mirror.discovered)

        accumulator = Accumulator()
        _dispatch_tick(resp.get("events") or [], mirror, accumulator,
                       err_counter, event_counter)

        # Drain the accumulator into intents (the SDK's own path), record command
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

    return {
        "ticks": ticks,
        "tick": mirror.tick,
        "robots_alive": len(mirror.robots),
        "robots_destroyed": mirror.destroyed,
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
def _format_summary(s: dict) -> str:
    """Render a :func:`run_local` summary as a readable, PASS/FAIL block."""
    def _counts(d: dict) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted(d.items())) or "—"

    errors = s.get("handler_errors", 0)
    destroyed = s.get("robots_destroyed", 0)
    ok = errors == 0
    lines = [
        "LOCAL-RUN SUMMARY",
        f"  ticks run        : {s.get('tick', 0)} / {s.get('ticks', 0)}",
        f"  robots alive     : {s.get('robots_alive', 0)}",
        f"  robots destroyed : {destroyed}",
        f"  buildings        : {_counts(s.get('buildings') or {})}",
        f"  base level       : {s.get('base_level')}",
        f"  handler errors   : {errors}",
        f"  map revealed     : {s.get('discovered_start', 0)} -> {s.get('discovered_end', 0)} cells",
        f"  commands issued  : {_counts(s.get('commands') or {})}",
        f"  events seen      : {_counts(s.get('events') or {})}",
    ]
    if ok:
        lines.append(f"LOCAL-CHECK: PASS — 0 handler errors ({destroyed} robots destroyed)")
    else:
        lines.append(f"LOCAL-CHECK: FAIL — {errors} handler error(s); scroll up for tracebacks")
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
                            module=args.module)
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
