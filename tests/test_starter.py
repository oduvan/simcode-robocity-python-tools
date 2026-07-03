"""A fresh ~300-tick run of the shipped starter behaves sensibly:
robots move, energy drains and recharges, the map grows, no exceptions."""

from conftest import starter_path
from robocity_sim.driver import load_user_module, Simulation


def test_starter_behaves():
    load_user_module(starter_path())
    sim = Simulation(city="local", seed=7)

    start_discovered = len(sim.mod.wd.discovered)
    positions = {}
    moved = False
    energies = []

    for t in range(1, 301):
        sim.step(t)  # must not raise
        for rid, r in sim.mod.wd.robots.items():
            prev = positions.get(rid)
            if prev is not None and prev != r.pos:
                moved = True
            positions[rid] = r.pos
            if rid == "r1":
                energies.append(r.energy)

    assert moved, "robots never changed position over 300 ticks"
    assert len(sim.mod.wd.discovered) > start_discovered, "map never grew"
    assert len(sim.mod.wd.robots) >= sim.cfg.num_start_robots

    # Energy drained below full at some point (flight) and returned to full
    # (recharge at the Base, which doubles as a charging pad).
    assert min(energies) < sim.cfg.energy_cap, "energy never dropped (no flight?)"
    assert max(energies) >= sim.cfg.energy_cap, "energy never returned to full (no recharge?)"


def test_run_completes_and_summarizes():
    from robocity_sim.driver import run_simulation
    res = run_simulation(starter_path(), ticks=300, seed=7)
    assert res.summary["final_tick"] == 300
    assert res.summary["robots"] >= 2
    assert res.summary["discovered_cells"] > 0
