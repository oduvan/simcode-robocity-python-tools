"""Subscription semantics: confirm the cases a real controller relies on behave
faithfully. The local driver dispatches every emitted event to the registry, so
handlers registered at import (the normal pattern) are exactly equivalent to the
server. This pins the three cases that could plausibly matter:
  - `once` fires exactly once,
  - `idle` re-emits (a passive handler keeps getting events → no permanent stall),
  - a handler subscribed MID-RUN receives subsequent events.

The only server behavior NOT reproduced is the *instantaneous* replay when a
handler subscribes to spawn/idle mid-run (the server injects an immediate catch-up
event); here that handler instead gets the next emission a few ticks later. That
distinction is documented in CLAUDE.md and is irrelevant to import-time handlers.
"""

from simcode import on, subscribe, robots
from simcode._registry import registry
from robocity_sim.driver import Simulation


def _run(ticks):
    sim = Simulation(seed=7)
    for t in range(1, ticks + 1):
        sim.step(t)
    return sim


def test_once_fires_exactly_once():
    registry.clear()
    calls = []
    subscribe("idle", lambda e: calls.append(e.robot_id), once=True)
    _run(20)
    assert len(calls) == 1, f"a once handler fired {len(calls)} times"


def test_idle_reemits_for_a_passive_handler():
    registry.clear()
    seen = []

    @on.idle
    def watch(e):
        seen.append(e.robot_id)  # do nothing → robot stays idle

    _run(30)
    # idle re-emits every IdleResendTicks, so a do-nothing controller keeps
    # receiving events instead of stalling after the first transition.
    assert len(seen) >= 4, f"idle did not re-emit (only {len(seen)} events)"


def test_dynamic_subscribe_midrun_receives_later_events():
    registry.clear()
    late = []

    @on.idle
    def bootstrap(e):
        if not getattr(bootstrap, "armed", False):
            bootstrap.armed = True
            subscribe("idle", lambda ev: late.append(ev.robot_id))

    _run(30)
    assert late, "a handler subscribed mid-run never received a later idle event"


def test_handler_exceptions_are_surfaced_not_swallowed():
    # A crashing handler must not kill the run (as on the server), but the tool
    # MUST report it — the whole point of testing locally is to catch the bug.
    registry.clear()

    @on.idle
    def buggy(e):
        raise ValueError("boom")

    sim = Simulation(seed=7)
    for t in range(1, 10):
        sim.step(t)
    assert sim.errors, "a raising handler was swallowed silently"
    assert sim.summary(9)["handler_errors"] == len(sim.errors)
    assert "boom" in sim.errors[0].get("error", "")
