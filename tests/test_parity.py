"""Cross-engine PARITY: this Python port must reproduce the golden fixture
generated from the authoritative server engine (game/modules/robot_city).

fixtures/parity-seed7.json is a byte-for-byte copy of the platform repo's
testdata/parity-seed7.json (the real Go engine's seed-7 starting world + key
config). If this diverges, the local sim has drifted from the server — a change
to the Go engine wasn't ported here (see the repo CLAUDE.md). Regenerate the
fixture on the platform side with PARITY_WRITE=1 and copy it into this repo.
"""

import json
import os

from robocity_sim.config import default_config
from robocity_sim.world import World

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "parity-seed7.json")


def _fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _spots(seed, region):
    wd = World(default_config())
    wd.generate("parity", seed)
    out = []
    for x in range(-region, region + 1):
        for y in range(-region, region + 1):
            sp = wd.cell_at(x, y).spot
            if sp is not None and sp.remaining > 0:
                out.append({"x": x, "y": y, "resource": sp.resource, "remaining": sp.remaining})
    return out


def test_worldgen_spots_match_server():
    fx = _fixture()
    assert _spots(fx["seed"], fx["region"]) == fx["spots"]


def test_initial_robots_match_server():
    fx = _fixture()
    wd = World(default_config())
    wd.generate("parity", fx["seed"])
    robots = sorted(
        (
            {
                "id": r.id,
                "x": float(r.pos[0]),
                "y": float(r.pos[1]),
                "energy": float(r.energy),
                "ore": r.ore,
                "metal": r.metal,
                "cap": r.cap,
            }
            for r in (wd.robots[i] for i in wd.robot_ord)
        ),
        key=lambda d: d["id"],
    )
    assert robots == fx["robots"]


def test_initial_buildings_match_server():
    fx = _fixture()
    wd = World(default_config())
    wd.generate("parity", fx["seed"])
    builds = sorted(
        (
            {"id": b.id, "type": b.typ, "x": b.pos[0], "y": b.pos[1],
             "w": b.w, "h": b.h, "cap": b.cap}
            for b in (wd.buildings[i] for i in wd.build_ord)
        ),
        key=lambda d: d["id"],
    )
    assert builds == fx["buildings"]


def test_config_matches_server():
    fx = _fixture()["config"]
    c = default_config()
    scalars = [
        "spot_density", "spot_rich_min", "spot_rich_max", "initial_reveal", "move_reveal",
        "fly_speed", "energy_cap", "energy_per_distance", "charge_rate",
        "carry_capacity", "num_start_robots", "start_ore", "start_metal",
        "produced_ore", "produced_metal", "mining_speed", "mining_storage_cap",
        "storage_cap", "base_storage_cap", "idle_resend_ticks",
    ]
    for name in scalars:
        assert getattr(c, name) == fx[name], f"config.{name} drifted from server"

    recipes = {
        "mining_recipe": c.recipes["mining"],
        "storage_recipe": c.recipes["storage"],
        "flying_station_recipe": c.recipes["flying_station"],
        "robot_recipe": c.robot_recipe,
    }
    for key, rec in recipes.items():
        want = fx[key]
        assert (rec.ore, rec.metal, rec.build_ticks) == (
            want["ore"], want["metal"], want["build_ticks"]
        ), f"config.{key} drifted from server"
