"""Autonomous mining: a mine fills its own store at MiningSpeed/tick, capped at
MiningStorageCap. Verified both at the mechanic level and end-to-end through a
controller that places the mine via world.build."""

from conftest import mine_path
from robocity_sim.config import default_config
from robocity_sim.module import (
    Module, Building, BUILDING_MINING, STATUS_ACTIVE,
)
from robocity_sim.world import Spot
from robocity_sim.driver import load_user_module, Simulation


def test_autonomous_mining_mechanic():
    cfg = default_config()
    m = Module(cfg)
    m.reset_world("t", 7)
    # A live spot + an active mine bound to it (mirrors the Go module test).
    m.wd.cell_at(4, 4).spot = Spot("ore", 100)
    m.wd.discovered[(4, 4)] = True
    m.wd.grow_bounds(4, 4)
    mine = Building(id="mine-1", typ=BUILDING_MINING, pos=(4, 4),
                    status=STATUS_ACTIVE, has_storage=True, cap=cfg.mining_storage_cap)
    mine.spot_cell = (4, 4)
    m.wd.add_building(mine)

    for t in range(1, 6):
        m.advance(t)
    assert mine.ore == 5 * cfg.mining_speed, mine.ore

    # Runs until the cap, then stops (buffer is capped).
    for t in range(6, 40):
        m.advance(t)
    assert mine.ore == cfg.mining_storage_cap, mine.ore
    assert mine.ore + mine.metal <= cfg.mining_storage_cap


def test_controller_builds_mine_and_it_fills():
    cfg = default_config()
    load_user_module(mine_path())
    sim = Simulation(city="local", cfg=cfg, seed=7)

    # Robots now spawn EMPTY (the boot stock lives on the Base, which is
    # production-only and can't be withdrawn), so hand the starting fleet a build
    # kit directly — mirrors the old spawn state this end-to-end path relies on
    # (one robot places the mine site, another drops its kit to complete it).
    for r in sim.mod.wd.robots.values():
        r.ore, r.metal = 6, 3

    # Inject a rich ore spot inside the starting reveal so r1 can reach + mine it.
    sim.mod.wd.cell_at(2, 0).spot = Spot("ore", 100)
    sim.mod.wd.discovered[(2, 0)] = True
    sim.mod.wd.grow_bounds(2, 0)

    for t in range(1, 160):
        sim.step(t)

    mines = [b for b in sim.mod.wd.buildings.values()
             if b.typ == BUILDING_MINING and b.status == STATUS_ACTIVE]
    assert mines, "controller never completed a mine"
    mine = mines[0]
    assert mine.pos == (2, 0)
    # The autonomous mine extracted ore into its capped store (hauling may draw
    # it down, so just require it stayed within the cap and mined something).
    assert sim.mod.wd.ore_mined > 0, "no ore was ever mined"
    assert mine.ore <= cfg.mining_storage_cap
