"""Minimal starter controller — keep the robots alive and explore the map.

Mirrors the shipped starter template: it does the smallest useful thing — fly
into the fog to reveal the map and recharge before the battery runs dry — and
deliberately does NOT mine, haul, or play the objective. It doubles as the
smoke-test target for the local engine runner (``python -m simcode.local
examples/starter_main.py``): a bare, item-model-agnostic controller that should
drive the real engine with zero handler errors.
"""

from simcode import on, robots

# Compass headings; a robot advances one per trip (kept in its memory) so the
# fleet fans out instead of re-treading one line into the fog.
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]

EXPLORE_HOP = 5     # world units to fly per exploration step
CHARGE_MARGIN = 15  # spare battery to keep on top of the trip home


@on.idle
def act(e):
    r = robots[e.robot_id]

    # Stay alive: a robot that runs its battery to zero mid-flight is destroyed,
    # so head back to the Base (origin, doubles as a charging pad) to recharge
    # WHILE there's still enough energy to reach it.
    if r.energy is not None and r.position is not None:
        x, y = r.position
        home = (x * x + y * y) ** 0.5  # distance to the Base at (0, 0)
        if r.energy < home + CHARGE_MARGIN:
            if r.cell == (0, 0):
                r.charge()
            else:
                r.move_to(0, 0)
            return

    # Otherwise explore: fly a short hop along a rotating heading. Flying reveals
    # the map (~5 cells around the robot), uncovering resource spots.
    n = r.memory.get("hop", 0) + 1
    r.memory["hop"] = n
    dx, dy = DIRS[n % len(DIRS)]
    x, y = r.position or (0, 0)
    r.move_to(x + dx * EXPLORE_HOP, y + dy * EXPLORE_HOP)


if __name__ == "__main__":  # pragma: no cover
    from simcode import run

    run()
