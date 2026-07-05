"""Tuning config — a faithful port of
``game/modules/robot_city/config.go`` (DefaultConfig).

Keep these values in lockstep with the Go source. They are the whole reason a
local run matches the server: same numbers -> same evolution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


# Building-type constants (mirror of contract/schema.go).
BUILDING_BASE = "base"
BUILDING_MINING = "mining"
BUILDING_STORAGE = "storage"
BUILDING_FLYING_STATION = "flying_station"

# The module fixes the world seed: every city of this type shares one map.
# (game/cmd/game/main.go: canonicalSeed = 7)
CANONICAL_SEED = 7


@dataclass(frozen=True)
class Recipe:
    ore: int
    metal: int
    build_ticks: int


@dataclass
class Config:
    # World generation (endless: generated lazily as discovered).
    spot_density: float = 0.025
    spot_rich_min: int = 150
    spot_rich_max: int = 600

    # Fog of war.
    initial_reveal: int = 4
    move_reveal: int = 5

    # Flight & energy (energy is spent ONLY on flying).
    fly_speed: float = 3.0
    energy_cap: float = 100.0
    energy_per_distance: float = 1.0
    charge_rate: float = 10.0

    # Robots.
    carry_capacity: int = 10
    num_start_robots: int = 2
    start_ore: int = 6
    start_metal: int = 3
    produced_ore: int = 6
    produced_metal: int = 3

    # Mining (autonomous).
    mining_speed: int = 1
    mining_storage_cap: int = 12

    # Storage / Base caps.
    storage_cap: int = 500
    base_storage_cap: int = 200

    # Reliability.
    idle_resend_ticks: int = 3

    # Construction recipes per building type (Base is not buildable).
    recipes: Dict[str, Recipe] = field(
        default_factory=lambda: {
            BUILDING_MINING: Recipe(ore=6, metal=3, build_ticks=4),
            BUILDING_STORAGE: Recipe(ore=3, metal=0, build_ticks=3),
            BUILDING_FLYING_STATION: Recipe(ore=4, metal=2, build_ticks=3),
        }
    )

    # Robot production at the Base (consumes the Base's reserved store).
    robot_recipe: Recipe = Recipe(ore=12, metal=6, build_ticks=8)


def default_config() -> Config:
    """The provisional v1 tuning values (== Go DefaultConfig())."""
    return Config()
