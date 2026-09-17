"""Regressions for the forum reports fixed together.

#26 / #27 / #28 — the default server was the HUB. `simgit.io` answers every
/api/* path with the SPA's index.html at HTTP 200, so the tool did not get an
error, it got HTML and died parsing it as JSON. Reported three times.

#30 — `inspect` printed `discovered_cells` as the number of RLE RUNS. One city
read 40 when it had discovered 1198.
"""
import os
from unittest import mock

from simcode import _engine_dl
from robocity_sim.cli import _discovered_cells, _status_from_snapshot


# --- #26 / #27 / #28 -------------------------------------------------------

def test_the_default_server_is_the_game_not_the_hub():
    assert _engine_dl.DEFAULT_SERVER == "https://robocity.simgit.io"


def test_the_default_server_is_never_the_bare_hub():
    # The precise failure: the hub's host serves the SPA on /api/*, so pointing
    # here produces HTML at HTTP 200 rather than an error anyone could read.
    host = _engine_dl.DEFAULT_SERVER.split("//", 1)[-1].split("/", 1)[0]
    assert host != "simgit.io"
    assert host.endswith(".simgit.io")


def test_server_base_uses_that_default_with_no_env():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _engine_dl.server_base() == "https://robocity.simgit.io"


def test_simcode_server_still_overrides_and_loses_a_trailing_slash():
    with mock.patch.dict(os.environ, {"SIMCODE_SERVER": "http://localhost:8080/"}):
        assert _engine_dl.server_base() == "http://localhost:8080"


# --- #30 -------------------------------------------------------------------

def test_discovered_cells_counts_cells_not_runs():
    # Runs are [y, x0, x1] with BOTH ends inclusive.
    assert _discovered_cells([[0, -19, 18]]) == 38
    assert _discovered_cells([[0, 0, 0]]) == 1
    assert _discovered_cells([[0, 0, 9], [1, 0, 9]]) == 20


def test_discovered_cells_on_nothing():
    assert _discovered_cells([]) == 0
    assert _discovered_cells(None) == 0


def test_discovered_cells_ignores_a_malformed_run():
    # Never raise inside `inspect` over one odd row.
    assert _discovered_cells([[0, 0, 4], [7]]) == 5


def test_inspect_summary_reports_cells():
    # The reporter's shape: several runs, far more cells than runs.
    snap = {
        "tick": 12,
        "world": {"seed": 1},
        "robots": [],
        "buildings": [],
        "discovered": [[y, -19, 18] for y in range(40)],  # 40 runs, 1520 cells
    }
    out = _status_from_snapshot("c", snap)
    assert out["discovered_cells"] == 1520, "counted runs, not cells"


# --- #31 -------------------------------------------------------------------

from robocity_sim.cli import _resume_caveats  # noqa: E402


def caveats(**w) -> str:
    return "\n".join(_resume_caveats(w))


def test_matching_engine_reads_as_a_verdict_not_two_strings():
    out = caveats(engine_version_source="save", engine_version="abc1234",
                  server_engine_version="abc1234")
    assert "engine check: OK" in out
    assert "NOT POSSIBLE" not in out


def test_a_mismatch_is_called_a_mismatch_and_says_why_it_matters():
    out = caveats(engine_version_source="save", engine_version="abc1234",
                  server_engine_version="def5678")
    assert "MISMATCH" in out
    assert "abc1234" in out and "def5678" in out
    # The consequence has to stay on screen: this is the silent-corruption case.
    assert "partly-zeroed world" in out


def test_a_save_with_no_version_still_says_it_cannot_check():
    # Every save written before the stamp shipped. Honest, not a guess.
    out = caveats(engine_version_source="none", engine_version="",
                  server_engine_version="abc1234")
    assert "NOT POSSIBLE" in out
