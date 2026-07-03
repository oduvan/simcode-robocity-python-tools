"""Robot City Builder — starter controller (Python).

The simplest thing that works: robots EXPLORE. Each one flies OUTWARD from the
Base to reveal the map, then flies back to recharge before its battery runs out —
and every trip it picks a NEW heading, so the fleet fans out across the whole area
instead of re-treading one line. The Base doubles as a charging pad, so there's
nothing to build — this is just "hello, world".

Whenever a robot is free it fires `idle`; you read its live state and give it its
next command. Robots FLY over float coordinates and spend ENERGY doing it (run dry
mid-flight and the robot is destroyed) — so we turn back to charge in time.

Make it do more — mine, haul, build a city. See CLAUDE.md for the full SDK.
"""

from simcode import buildings, on, robots

# Eight compass headings. A robot rotates through them (one per outbound trip) so
# successive trips sweep a fresh slice of the map — real exploration, not a shuttle.
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]

# Per-robot trip counter: bumped each time a robot leaves the Base, which advances
# its heading. Module state resets on a code reload — fine for exploring.
TRIP: dict = {}


@on.idle
def act(e):
    r = robots[e.robot_id]
    base = buildings.base
    if base is None:
        return
    bx, by = base.position
    x, y = r.position
    home = abs(x - bx) + abs(y - by)              # ~energy needed to fly back
    at_base = r.cell == (bx, by)

    # Turn back and charge while the battery can still get us home.
    if r.energy is not None and r.energy <= home + 15:
        if at_base:
            r.charge()
        else:
            r.log("low battery — returning to base to charge")
            r.move_to(bx, by)
        return

    # Starting a fresh trip from the Base → advance the heading so this outing
    # explores new ground instead of repeating the last one.
    if at_base:
        TRIP[r.id] = TRIP.get(r.id, 0) + 1
        r.log("charged — heading out to explore new ground")
    dx, dy = DIRS[(sum(map(ord, r.id)) + TRIP.get(r.id, 0)) % len(DIRS)]
    r.move_to(x + dx * 5, y + dy * 5)
