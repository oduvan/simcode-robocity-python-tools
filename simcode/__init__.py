"""simcode — Python SDK for the Robot City Builder module.

A city runs **one** user script that controls the whole fleet. The script
imports this package, registers event handlers, and issues command intents to
robots by id; the SDK runtime carries everything to/from GAME over Redis.

    from simcode import on, robots, buildings, world, run

    @on.spawn
    def start(e):
        robots[e.robot_id].move_to(0, 0)      # moving reveals the map (no scan)

    @on.idle
    def plan(e):
        # Out of work: head for the nearest known ore spot, else drift onward.
        r = robots[e.robot_id]
        spot = r.nearest(kind="ore_spot")
        x, y = r.position or (0, 0)
        r.move_to(*(spot if spot else (x + 1, y)))

    if __name__ == "__main__":
        run()                       # connect to Redis and dispatch forever

Public surface:
- ``on`` / ``subscribe`` / ``unsubscribe`` — event subscriptions.
- ``robots`` / ``buildings`` / ``world`` / ``store`` — the live read model
  (fetched fresh from Redis on every event) + command issuing.
- ``run`` / ``Runtime`` — the runtime the CODE container starts.
- ``wire`` / ``contract`` — the frozen wire-protocol mirror.

Security note (TODO): the user script is meant to run inside a platform-owned
restricted sandbox; only the SDK runtime touches Redis. The sandbox itself is
out of scope for this phase — see docs/sandbox-security.md.
"""

from . import _wire as wire
from . import contract
from ._proxies import buildings, robots, store, world
from ._registry import on, registry, subscribe, unsubscribe
from ._runtime import Runtime, run

__all__ = [
    "wire",
    "contract",
    "on",
    "subscribe",
    "unsubscribe",
    "robots",
    "buildings",
    "world",
    "store",
    "run",
    "Runtime",
    "registry",
]
__version__ = "0.1.0"
