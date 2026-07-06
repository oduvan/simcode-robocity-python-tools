"""Building-role redesign: the Base is the quest hub ONLY (no store, no robot
production) and the Flying Station is the charging pad + robot factory. Mirror of
the Go engine (module.go Submit, buildings.go advanceStationProduction,
commands.go doDrop/doPickUp)."""

from robocity_sim.config import (
    default_config, BUILDING_FLYING_STATION, BUILDING_STORAGE,
)
from robocity_sim.module import (
    Module, Intent, EVENT_BLOCKED, EVENT_ROBOT_PRODUCED,
)
from robocity_sim.world import Building


def _station(m, pos=(5, 0), ore=0, metal=0):
    """Drop an active Flying Station (with a production store) into the world."""
    wd = m.wd
    wd.next_build += 1
    b = Building(
        id=BUILDING_FLYING_STATION + "-" + str(wd.next_build),
        typ=BUILDING_FLYING_STATION, pos=pos, status="active",
        has_storage=True, cap=m.cfg.station_storage_cap,
    )
    b.ore, b.metal = ore, metal
    wd.add_building(b)
    return b


# --- starting world -------------------------------------------------------- #
def test_starting_world_base_empty_storage_holds_capital():
    m = Module(default_config())
    m.reset_world("t", 7)
    base = m.wd.base()
    assert base.has_storage is False
    assert base.ore == 0 and base.metal == 0
    assert base.level == 1
    # A pre-placed Storage at the anchor (2,0) holds the starting capital.
    storage = m.wd.building_at(2, 0)
    assert storage is not None and storage.typ == BUILDING_STORAGE
    assert storage.pos == (2, 0) and (storage.w, storage.h) == (2, 2)
    assert storage.ore == m.cfg.start_capital_ore
    assert storage.metal == m.cfg.start_capital_metal
    # Both starting robots spawn EMPTY.
    for r in m.wd.robots.values():
        assert r.ore == 0 and r.metal == 0


# --- robot production at a Flying Station ---------------------------------- #
def test_build_robot_targets_station_and_spawns_there_empty():
    m = Module(default_config())
    m.reset_world("t", 7)
    rr = m.cfg.robot_recipe
    st = _station(m, pos=(5, 0), ore=rr.ore, metal=rr.metal)
    # Queue one robot against THIS station's id.
    m.submit(Intent(robot=st.id, commands=[{"cmd": "build_robot", "args": [1]}], logs=[]), 1)
    assert st.prod_queue == 1
    produced = None
    for t in range(2, 2 + rr.build_ticks + 2):
        evs = m.advance(t)
        for e in evs:
            if e["event"] == EVENT_ROBOT_PRODUCED:
                produced = e["payload"]["robot_id"]
    assert produced is not None, "station should finish a robot"
    # Consumed the recipe from the station's OWN store.
    assert st.ore == 0 and st.metal == 0
    nr = m.wd.robots[produced]
    # Spawns AT the station, empty inventory, full energy.
    assert nr.pos == (5.0, 0.0)
    assert nr.ore == 0 and nr.metal == 0
    assert nr.energy == m.cfg.energy_cap


def test_build_robot_on_non_station_is_blocked_not_a_station():
    m = Module(default_config())
    m.reset_world("t", 7)
    base = m.wd.base()
    evs = m.submit(
        Intent(robot=base.id, commands=[{"cmd": "build_robot", "args": [1]}], logs=[]), 1
    )
    reasons = [e["payload"]["reason"] for e in evs if e["event"] == EVENT_BLOCKED]
    assert "not_a_station" in reasons
    assert base.prod_queue == 0


# --- Base quest-accumulator cap on drop ------------------------------------ #
def test_drop_at_base_caps_at_requirement_remainder_stays():
    m = Module(default_config())
    m.reset_world("t", 7)
    base = m.wd.base()  # level 1 -> quest 40/20
    r = next(iter(m.wd.robots.values()))
    r.pos = (0.0, 0.0)  # stand on the Base
    r.ore, r.metal = 50, 25  # more than the requirement
    m.submit(Intent(robot=r.id, commands=[{"cmd": "drop"}], logs=[]), 1)
    # Base accepts only up to the requirement; the remainder stays in the robot.
    assert base.ore == 40 and base.metal == 20
    assert r.ore == 10 and r.metal == 5


# --- Flying Station store: drop feeds it, pick_up is reserved --------------- #
def test_drop_at_station_feeds_production_store():
    m = Module(default_config())
    m.reset_world("t", 7)
    st = _station(m, pos=(5, 0))
    r = next(iter(m.wd.robots.values()))
    r.pos = (5.0, 0.0)
    r.ore, r.metal = 6, 3
    m.submit(Intent(robot=r.id, commands=[{"cmd": "drop"}], logs=[]), 1)
    assert st.ore == 6 and st.metal == 3
    assert r.ore == 0 and r.metal == 0


def test_pick_up_from_station_is_blocked_station_reserved():
    m = Module(default_config())
    m.reset_world("t", 7)
    st = _station(m, pos=(5, 0), ore=10, metal=5)
    r = next(iter(m.wd.robots.values()))
    r.pos = (5.0, 0.0)
    r.ore = r.metal = 0
    evs = m.submit(Intent(robot=r.id, commands=[{"cmd": "pick_up"}], logs=[]), 1)
    reasons = [e["payload"]["reason"] for e in evs if e["event"] == EVENT_BLOCKED]
    assert "station_reserved" in reasons
    assert r.ore == 0 and r.metal == 0  # nothing taken
    assert st.ore == 10 and st.metal == 5
