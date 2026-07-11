"""End-to-end smoke test against the REAL engine.

Gated on ``$SIMCODE_ENGINE_SO`` (a locally-built ``libengine-robot-city-*.so``) so
CI — which can't build the private engine — skips it, while a developer with a local
build (or the downloaded artifact) exercises the full ``run_local`` path: import the
starter, drive it against the actual engine, and assert it runs cleanly.
"""

import os

import pytest

SO = os.environ.get("SIMCODE_ENGINE_SO")
EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "starter_main.py")


@pytest.mark.skipif(not SO, reason="set SIMCODE_ENGINE_SO to a local engine .so to run")
def test_starter_runs_clean_against_real_engine():
    from simcode._local import run_local

    summary = run_local(EXAMPLE, seed=7, ticks=120, so_path=SO)
    assert summary["handler_errors"] == 0
    assert summary["robots_alive"] > 0
    # The minimal starter only explores, so it should reveal new ground.
    assert summary["discovered_end"] >= summary["discovered_start"]
