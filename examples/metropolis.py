"""Example controller: a continuously GROWING city (compounding loop).

One script drives the whole fleet by id in the redesigned world: robots FLY and
only HAUL; mining and construction are AUTONOMOUS (place a site with
``world.build`` and the building does the rest); robots CHARGE on a Flying Station
when low. Each delivered load lets a Flying Station build more robots, which place
more mines, so mines + robots + resources keep climbing.

The whole thing is driven by ``idle`` (each robot picks its next action from the
live state), with a throttled ``tick`` reconciler that keeps growth going and
re-tasks any robot that fell idle. Run: ``python -m examples.metropolis``.
"""

from simcode import buildings, on, robots, world

MINE_COST = (6, 3)          # a Mining site needs 6 ore + 3 metal
STATION_ROBOT = (12, 6)     # Flying Station recipe for one robot (afford check)
LOW_ENERGY = 30             # recharge below this

STEP: dict = {}             # robot id -> explore counter (fan out)


# --- reading the world ------------------------------------------------------

def _dist(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _built_cells():
    return {b.position for b in buildings.all()}


def _unbuilt_spot(r, want):
    built = _built_cells()
    cands = [s for s in world.spots()
             if s.position not in built and s.spot.remaining > 0]
    if not cands:
        return None
    cands.sort(key=lambda s: (s.spot.resource != want, _dist(r.position, s.position)))
    return cands[0].position


def _stocked_mine(r):
    mines = [b for b in buildings.of_type("mining")
             if b.status == "active" and b.storage.total >= 6]
    return min(mines, key=lambda b: _dist(r.position, b.position)) if mines else None


def _station(r):
    # The Base doubles as a charging pad, so it's always a valid charge point.
    pts = [b for b in buildings.all()
           if b.status == "active" and b.type in ("flying_station", "base")]
    return min(pts, key=lambda b: _dist(r.position, b.position)) if pts else None


def _home(r):
    """Nearest active Flying Station — robots are built and resupplied here
    (drop delivers to its production store)."""
    sts = [b for b in buildings.stations() if b.status == "active"]
    return min(sts, key=lambda b: _dist(r.position, b.position)) if sts else None


def _any_station():
    for b in buildings.stations():
        if b.status == "active":
            return b
    return None


def _mine_covers(res):
    return any(b.type == "mining" and b.spot and b.spot.resource == res
               for b in buildings.all())


def _needy_site(inv):
    for b in buildings.all():
        if b.status != "constructing":
            continue
        c = b.construction
        need_ore = c.required.get("ore", 0) - c.delivered.get("ore", 0)
        need_metal = c.required.get("metal", 0) - c.delivered.get("metal", 0)
        if (need_ore > 0 and inv["ore"] > 0) or (need_metal > 0 and inv["metal"] > 0):
            return b
    return None


def _goto(r, pos, then):
    if r.cell == tuple(pos):
        then()
    else:
        r.move_to(*pos)


def _explore(r):
    i = STEP.get(r.id, 0)
    STEP[r.id] = i + 1
    dx, dy = ((6, 0), (0, 6), (-6, 0), (0, -6))[(i + len(r.id)) % 4]
    x, y = r.position
    r.move_to(x + dx, y + dy)


def _grow(station):
    # Robots are built at a Flying Station (not the Base). Build when the
    # station's production store can afford one.
    if (station and station.storage["ore"] >= STATION_ROBOT[0]
            and station.storage["metal"] >= STATION_ROBOT[1]):
        station.build_robot(1)   # <-- growth driver


# --- the brain --------------------------------------------------------------

def _act(r):
    home = _home(r)
    if home is None:
        return
    inv = r.inventory

    # 0. Low battery -> charge at a Flying Station.
    st = _station(r)
    if r.energy is not None and r.energy <= LOW_ENERGY and st is not None:
        _goto(r, st.position, r.charge)
        return

    # 1. Holding a kit -> turn a spot into a Mining site (cover BOTH resources
    #    first — the Base needs ore AND metal to grow).
    if inv["ore"] >= MINE_COST[0] and inv["metal"] >= MINE_COST[1]:
        if not _mine_covers("metal"):
            want = "metal"
        elif not _mine_covers("ore"):
            want = "ore"
        else:
            want = "ore" if home.storage["ore"] <= home.storage["metal"] else "metal"
        spot = _unbuilt_spot(r, want)
        if spot is None:
            _explore(r)
        elif r.cell == spot:
            world.build("mining", *spot)
            r.drop("ore", MINE_COST[0])    # supply the site the mine recipe,
            r.drop("metal", MINE_COST[1])  # one item at a time (multi-item haul)
        else:
            r.move_to(*spot)
        return

    # 2. Carrying output -> deliver to a needy site, else the home Flying
    #    Station (+ grow — its store funds robot production).
    if inv.total > 0:
        site = _needy_site(inv)
        target = site.position if site else home.position
        if r.cell == tuple(target):
            r.drop()
            if not site:
                _grow(home)
        else:
            r.move_to(*target)
        return

    # 3. Empty -> haul a stocked mine's output.
    mine = _stocked_mine(r)
    if mine is not None:
        _goto(r, mine.position, r.pick_up)
        return

    # 4. Nothing to haul -> explore to reveal fresh spots (a Flying Station is
    #    always pre-placed by the Base, so we never need to build one).
    _explore(r)


@on.spawn
def begin(e):
    _act(robots[e.robot_id])


@on.idle
def on_idle(e):
    _act(robots[e.robot_id])


@on.blocked
def on_blocked(e):
    _act(robots[e.robot_id])


@on.robot_destroyed
def on_destroyed(e):
    # A robot flew out of energy and was lost — the Base will produce more.
    pass


@on.spot_depleted
def on_depleted(e):
    # A mine exhausted its spot; idle robots will pick a different one.
    pass


@on.construction_complete
def on_built(e):
    pass


@on.tick
def reconcile(e):
    """Throttled safety net: keep growth going and re-task any idle robot."""
    if getattr(e, "tick_no", 0) % 8:
        return
    _grow(_any_station())
    for r in robots.all():
        if r.state == "idle":
            _act(r)


if __name__ == "__main__":  # pragma: no cover
    from simcode import run

    run()
