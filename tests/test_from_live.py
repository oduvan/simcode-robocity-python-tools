"""#73 req 1: run from the city's CURRENT state, not only from a fresh world.

A push never starts a new world. It loads new code into a running, mature city
where in-memory values are reset, saved values survive, and robots are part-way
through journeys carrying cargo. `--from-live` reproduces exactly that.

These tests use a fake ``/api/city/<slug>/save`` document, so they need no engine
and no network.
"""

import argparse
import urllib.error

import pytest

from robocity_sim import cli, live


SAVE_DOC = {
    "slug": "my-city", "saved": True, "type": "robot-city",
    "seed": 100057250, "config": {"starting_fleet": "3"},
    "tick": 14460, "seq": 14460,
    "engine_version": "", "engine_version_source": "none",
    "server_engine_version": "e191ef9",
    "save": {"tick": 14460, "seq": 14460, "world": {"seed": 100057250}},
    "store": {"claims": {"r1": "mining-4"}, "version": "abc123"},
}

SNAPSHOT_DOC = {"tick": 14562, "robots": [{"id": "r1"}], "buildings": [], "spots": [],
                "discovered": [], "stats": {}}


def _args(**kw):
    base = dict(controller="main.py", ticks=10, seed=None, canonical=False,
                from_live=True, module="robot-city", json=False, city="my-city",
                skip_code_check=True, server="https://example.test")
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def live_city(monkeypatch):
    monkeypatch.setattr("robocity_sim.live.saved_world_of_city",
                        lambda s, slug: SAVE_DOC)
    monkeypatch.setattr("robocity_sim.live.public_snapshot",
                        lambda s, slug: SNAPSHOT_DOC)


def test_from_live_carries_the_save_the_store_and_the_identity(live_city):
    w = cli._resolve_world(_args())

    # The whole save envelope goes to the engine as its map state, unconverted.
    assert w["map_state"] == SAVE_DOC["save"]
    assert w["save_tick"] == 14460
    # The store lives OUTSIDE the world and must come along, or the city looks
    # right and behaves wrong.
    assert w["store"] == SAVE_DOC["store"]
    assert w["store_keys"] == 2
    assert w["seed"] == 100057250
    assert w["config"] == {"starting_fleet": "3"}
    assert w["module_type"] == "robot-city"


def test_from_live_names_itself_and_the_tick_it_resumes_at(live_city):
    w = cli._resolve_world(_args())
    assert "AS IT IS NOW" in w["origin"] and "my-city" in w["origin"]
    assert "resumed at tick 14461" in w["start"]
    assert "14460" in w["start"]


def test_from_live_is_not_the_default(live_city):
    """A cold start stays the default: reproducible, and it works with no city."""
    monkeypatchless = _args(from_live=False)
    import robocity_sim.live as L
    calls = []
    orig = L.world_of_city
    L.world_of_city = lambda s, slug: (calls.append(slug) or (7, None))
    try:
        w = cli._resolve_world(monkeypatchless)
    finally:
        L.world_of_city = orig
    assert w.get("map_state") is None
    assert w["start"] == "fresh world at tick 0"


def test_no_save_stops_rather_than_substituting_a_fresh_world(monkeypatch):
    """A city that has never checkpointed is NOT an empty world to invent."""
    monkeypatch.setattr("robocity_sim.live._http_get_json",
                        lambda url: (_ for _ in ()).throw(
                            _http_error(404, b'{"reason":"no_save","error":"no save"}')))

    with pytest.raises(live.WorldUnavailable) as exc:
        live.saved_world_of_city("https://example.test", "c")
    assert "no saved world yet" in str(exc.value)
    assert "--from-live" in str(exc.value)  # points at the way forward


def test_unknown_city_stops(monkeypatch):
    monkeypatch.setattr("robocity_sim.live._http_get_json",
                        lambda url: (_ for _ in ()).throw(
                            _http_error(404, b'{"reason":"unknown_city","error":"nope"}')))
    with pytest.raises(live.WorldUnavailable) as exc:
        live.saved_world_of_city("https://example.test", "ghost")
    assert "no city 'ghost'" in str(exc.value)


def test_unreachable_server_stops(monkeypatch):
    monkeypatch.setattr("robocity_sim.live._http_get_json",
                        lambda url: (_ for _ in ()).throw(OSError("connection refused")))
    with pytest.raises(live.WorldUnavailable) as exc:
        live.saved_world_of_city("https://example.test", "c")
    assert "could not reach" in str(exc.value)


def test_a_save_without_a_world_is_refused(monkeypatch):
    monkeypatch.setattr("robocity_sim.live._http_get_json",
                        lambda url: {"save": {"tick": 1}})
    with pytest.raises(live.WorldUnavailable):
        live.saved_world_of_city("https://example.test", "c")


def test_missing_snapshot_stops_instead_of_running_a_blind_controller(monkeypatch):
    """Without the read model seeded, handlers would see an almost empty world —
    which would look like a working run. Refuse instead."""
    monkeypatch.setattr("robocity_sim.live.saved_world_of_city", lambda s, slug: SAVE_DOC)
    monkeypatch.setattr("robocity_sim.live.public_snapshot",
                        lambda s, slug: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(live.WorldUnavailable) as exc:
        cli._resolve_world(_args())
    assert "almost empty world" in str(exc.value)


def test_run_exits_6_when_the_live_state_cannot_be_obtained(monkeypatch, tmp_path, capsys):
    entry = tmp_path / "main.py"
    entry.write_text("from simcode import on\n")
    monkeypatch.setattr("robocity_sim.live.saved_world_of_city",
                        lambda s, slug: (_ for _ in ()).throw(
                            live.WorldUnavailable("no saved world yet")))
    rc = cli.cmd_run(_args(controller=str(entry)))
    assert rc == 6
    assert "will not run a different world" in capsys.readouterr().err


def test_banner_states_the_unverifiable_engine_and_the_read_model_skew(live_city, capsys):
    w = cli._resolve_world(_args())
    cli._print_world_banner(w, "robot-city", 100)
    out = capsys.readouterr().out

    # The engine check genuinely cannot be done today; say so rather than imply one.
    assert "engine check: NOT POSSIBLE" in out
    assert "partly-zeroed world WITHOUT any error" in out
    # And the read model is seeded from a newer display state — 102 ticks here.
    assert "102 tick(s) NEWER" in out
    assert "2 saved key(s) restored" in out


def test_seed_canonical_and_from_live_are_mutually_exclusive():
    for extra in (["--seed", "1"], ["--canonical"]):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["run", "main.py", "--from-live"] + extra)


# --- helpers ---------------------------------------------------------------

def _http_error(code, body):
    err = urllib.error.HTTPError("u", code, "err", None, None)
    err.read = lambda: body
    return err
