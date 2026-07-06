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


@dataclass(frozen=True)
class Footprint:
    """A building type's rectangular size in cells (w wide, h tall).

    A building's anchor (pos) is the MIN corner; it occupies every cell in
    [x, x+w) x [y, y+h). A robot standing on ANY covered cell can interact
    with it. Any type not listed defaults to 1x1 (see Config.footprint).
    """

    w: int
    h: int


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
    start_ore: int = 0  # robots spawn EMPTY — the boot stock lives in a Storage now
    start_metal: int = 0
    # produced_ore/produced_metal are RETAINED but currently UNUSED: a
    # station-produced robot now spawns EMPTY. Kept so the kit can be
    # reintroduced without a config/parity-schema change.
    produced_ore: int = 6
    produced_metal: int = 3

    # Starting capital: the boot stock lives in a pre-placed Storage next to the
    # Base at world start (the Base itself no longer seeds a store).
    start_capital_ore: int = 30
    start_capital_metal: int = 15

    # Mining (autonomous).
    mining_speed: int = 1
    mining_storage_cap: int = 12

    # Storage caps.
    storage_cap: int = 500
    # A Flying Station's robot-production store cap.
    station_storage_cap: int = 200
    # base_storage_cap is RETAINED but currently UNUSED: the Base's store is the
    # quest accumulator, capped PER-RESOURCE at quest_for(level), not by this value.
    base_storage_cap: int = 200

    # Reliability.
    idle_resend_ticks: int = 3

    # Base quests (the game objective). The Base starts at level 1; each level
    # poses a quest = a required amount of raw ore+metal that must accumulate in
    # the Base's quest store (drops are capped per-resource at the requirement).
    # When both are met, the store RESETS to 0 and the Base levels up to the
    # next, harder quest. questFor(level) escalates the requirement
    # geometrically from the base amounts by quest_growth_num/quest_growth_den
    # per level. (Mirror of config.go.)
    quest_base_ore: int = 40
    quest_base_metal: int = 20
    quest_growth_num: int = 3
    quest_growth_den: int = 2

    # Construction recipes per building type (Base is not buildable).
    recipes: Dict[str, Recipe] = field(
        default_factory=lambda: {
            BUILDING_MINING: Recipe(ore=6, metal=3, build_ticks=4),
            BUILDING_STORAGE: Recipe(ore=3, metal=0, build_ticks=3),
            BUILDING_FLYING_STATION: Recipe(ore=4, metal=2, build_ticks=3),
        }
    )

    # Footprints per building type. Any type not listed defaults to 1x1 (see
    # footprint). Storage is a 2x2 hub; base/mining/flying_station stay 1x1.
    footprints: Dict[str, Footprint] = field(
        default_factory=lambda: {
            BUILDING_STORAGE: Footprint(w=2, h=2),
        }
    )

    # Robot production at a Flying Station (consumes that station's own store).
    robot_recipe: Recipe = Recipe(ore=12, metal=6, build_ticks=8)

    def footprint(self, typ: str) -> tuple[int, int]:
        """The (w, h) cell footprint for a building type, default 1x1."""
        f = self.footprints.get(typ)
        if f is not None and f.w > 0 and f.h > 0:
            return f.w, f.h
        return 1, 1

    def quest_for(self, level: int) -> tuple[int, int]:
        """The (ore, metal) the Base must accumulate to clear the quest at the
        given level (level 1 = the base amounts, each subsequent level scaled by
        quest_growth_num/quest_growth_den). Pure + deterministic integer math, so
        it reproduces the Go engine exactly. Level < 1 is treated as 1.
        (Mirror of config.go questFor.)"""
        if level < 1:
            level = 1
        ore, metal = self.quest_base_ore, self.quest_base_metal
        num, den = self.quest_growth_num, self.quest_growth_den
        if num <= 0 or den <= 0:
            return ore, metal
        for _ in range(1, level):
            ore = ore * num // den
            metal = metal * num // den
        return ore, metal


def default_config() -> Config:
    """The provisional v1 tuning values (== Go DefaultConfig())."""
    return Config()
