"""hashCell + world-generation determinism (matches the Go module_test
TestWorldGenDeterministicAndBalanced for seed 7)."""

from robocity_sim.world import World, hash_cell
from robocity_sim.config import default_config


def _spots(seed):
    wd = World(default_config())
    wd.generate("g", seed)
    out = {}
    for y in range(-20, 21):
        for x in range(-20, 21):
            sp = wd.cell_at(x, y).spot
            if sp is not None:
                out[(x, y)] = sp.resource
    return out


def test_hashcell_is_pure_and_masked():
    # Stable across calls; result fits in 64 bits.
    assert hash_cell(7, 3, -4) == hash_cell(7, 3, -4)
    for x, y in [(0, 0), (5, 5), (-1, -1), (12, -30), (-100, 100)]:
        h = hash_cell(7, x, y)
        assert 0 <= h < (1 << 64)


def test_seed7_spots_stable_and_balanced():
    a = _spots(7)
    b = _spots(7)
    assert a == b, "spot placement must be deterministic across runs"
    assert len(a) >= 5, f"expected several spots in the field, got {len(a)}"
    ore = sum(1 for v in a.values() if v == "ore")
    metal = sum(1 for v in a.values() if v == "metal")
    assert ore >= 1 and metal >= 1, f"world gen must place both (ore={ore} metal={metal})"


def test_different_seed_differs():
    a = _spots(7)
    d = _spots(99)
    assert a != d, "different seeds should generate different fields"
