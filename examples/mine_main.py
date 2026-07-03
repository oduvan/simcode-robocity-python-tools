"""A tiny scripted controller used by the test-suite: place ONE mine on the
nearest known ore spot, feed it the starting kit, then keep hauling its output
back to the Base. Deliberately minimal (not a good city) — it exercises the
build -> autonomous-mine -> haul path end to end.
"""

from simcode import on, robots, world, buildings


@on.idle
def act(e):
    r = robots[e.robot_id]
    base = buildings.base
    bx, by = base.position if base else (0, 0)

    # Standing on a known ore spot with a free cell and a kit -> place a mine.
    spot = r.here.spot
    if spot is not None and spot.get("resource") == "ore" and r.here.building is None:
        if r.inventory.ore >= 6 and r.inventory.metal >= 3:
            world.build("mining", r.cell[0], r.cell[1])
            r.drop()
            return

    # A mine on our cell with output -> pick it up and haul it to the Base.
    here = r.here.building
    if here is not None and here.type == "mining" and here.status == "active":
        if here.storage.ore > 0 and not r.inventory.is_full:
            r.pick_up()
            return
    if r.cell == (bx, by) and r.inventory.ore > 0:
        r.drop()
        return

    # Otherwise: go to the nearest known ore spot, else sit tight near base.
    target = r.nearest(kind="ore_spot")
    if target is not None and r.cell != tuple(target):
        r.move_to(target[0], target[1])
        return
    # Carrying output? head home to drop it.
    if r.inventory.ore > 0 and r.cell != (bx, by):
        r.move_to(bx, by)
