"""Same seed twice -> byte-identical event feed and final summary."""

from conftest import starter_path
from robocity_sim.driver import run_simulation


def test_starter_run_is_reproducible():
    a = run_simulation(starter_path(), ticks=200, seed=7)
    b = run_simulation(starter_path(), ticks=200, seed=7)
    assert a.feed == b.feed, "activity feed diverged between identical runs"
    assert a.summary == b.summary, "final summary diverged between identical runs"


def test_seed_changes_outcome():
    a = run_simulation(starter_path(), ticks=200, seed=7)
    c = run_simulation(starter_path(), ticks=200, seed=8)
    # Different seed -> different discovered map / feed (world gen is seeded).
    assert a.feed != c.feed or a.summary != c.summary
