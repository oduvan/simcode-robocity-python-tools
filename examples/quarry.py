"""Minimal example: place a mine, then haul its output (autonomous mining).

A tiny but correct illustration of the redesigned SDK shape: robots FLY (float
coordinates), mining is AUTONOMOUS — you place a site with ``world.build(...)``
and the Mining building digs on its own — and robots only HAUL. The richer
compounding demo lives in ``metropolis.py``. Run: ``python -m examples.quarry``.
"""

from simcode import buildings, on, robots, world

MINE_COST = {"ore": 6, "metal": 3}  # a Mining site needs 6 ore + 3 metal


@on.idle
def act(e):
    r = robots[e.robot_id]
    base = buildings.base
    if base is None:
        return
    inv = r.inventory

    # Holding a starter kit -> fly to a bare spot, place a mine, drop the recipe.
    if inv["ore"] >= MINE_COST["ore"] and inv["metal"] >= MINE_COST["metal"]:
        spot = _bare_spot()
        if spot is None:
            _drift(r)
        elif r.cell == spot:
            world.build("mining", *spot)   # self-builds once supplied
            r.drop()                        # feed the site the kit we're holding
        else:
            r.move_to(*spot)
        return

    # Carrying mined output -> haul it to the Base.
    if inv.total > 0:
        if r.cell == tuple(base.position):
            r.drop()
        else:
            r.move_to(*base.position)
        return

    # Empty -> pick up from a stocked mine, else drift to reveal more map.
    mine = _stocked_mine()
    if mine is None:
        _drift(r)
    elif r.cell == tuple(mine.position):
        r.pick_up()
    else:
        r.move_to(*mine.position)


def _bare_spot():
    built = {b.position for b in buildings.all()}
    for s in world.spots():
        if s.position not in built and s.spot.remaining > 0:
            return s.position
    return None


def _stocked_mine():
    for b in buildings.of_type("mining"):
        if b.status == "active" and b.storage.total > 0:
            return b
    return None


def _drift(r):
    x, y = r.position or (0, 0)
    r.move_to(x + 3, y)


if __name__ == "__main__":  # pragma: no cover
    from simcode import run

    run()
