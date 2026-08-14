"""#73 req 2: a run never silently substitutes a different world.

Forum post 22: two identical invocations produced two different worlds — 3
starting robots vs 2, a different quest ladder — because a failed city lookup
quietly fell back to the canonical map (seed 7), with exit code 0 and one changed
word in the banner.

These tests pin the replacement behaviour: a lookup failure STOPS the run, and a
different world is only ever used when it was asked for by name.
"""

import argparse

import pytest

from robocity_sim import cli
from robocity_sim.live import CANONICAL_SEED, WorldUnavailable


def _args(**kw):
    base = dict(controller="main.py", ticks=10, seed=None, canonical=False, from_live=False,
                module="robot-city", json=False, city=None,
                skip_code_check=True, server="https://example.test")
    base.update(kw)
    return argparse.Namespace(**base)


def test_explicit_seed_is_named_as_such():
    world = cli._resolve_world(_args(seed=1234))
    assert world["seed"] == 1234
    assert "explicit --seed 1234" in world["origin"]


def test_canonical_must_be_asked_for_by_name():
    world = cli._resolve_world(_args(canonical=True))
    assert world["seed"] == CANONICAL_SEED
    assert "canonical" in world["origin"]


def test_city_world_carries_seed_and_config(monkeypatch):
    monkeypatch.setattr(cli, "_project_dir", lambda c: ".")
    monkeypatch.setattr("robocity_sim.live.world_of_city",
                        lambda s, slug: (100057250, {"starting_fleet": 5}))
    world = cli._resolve_world(_args(city="my-city"))
    assert world["seed"] == 100057250
    assert world["config"] == {"starting_fleet": 5}
    assert "my-city" in world["origin"]


def test_unreachable_server_stops_instead_of_using_another_world(monkeypatch):
    """THE regression: the snapshot fetch fails, and the answer is an error —
    not seed 7."""
    def boom(server, slug):
        raise WorldUnavailable("could not reach https://example.test")
    monkeypatch.setattr("robocity_sim.live.world_of_city", boom)

    with pytest.raises(WorldUnavailable):
        cli._resolve_world(_args(city="my-city"))


def test_run_exits_nonzero_when_the_world_cannot_be_obtained(monkeypatch, tmp_path, capsys):
    entry = tmp_path / "main.py"
    entry.write_text("from simcode import on\n")

    def boom(server, slug):
        raise WorldUnavailable("could not reach https://example.test")
    monkeypatch.setattr("robocity_sim.live.world_of_city", boom)

    rc = cli.cmd_run(_args(controller=str(entry), city="my-city"))
    assert rc == 6  # a distinct code: "I could not get the world you asked for"
    err = capsys.readouterr().err
    assert "will not run a different world" in err
    assert "--canonical" in err  # tells you how to ask for another world on purpose


def test_no_repo_and_no_flags_stops_rather_than_guessing(monkeypatch, tmp_path):
    monkeypatch.setattr("robocity_sim.live.git_repo_slug", lambda d: None)
    with pytest.raises(WorldUnavailable):
        cli._resolve_world(_args(controller=str(tmp_path / "main.py")))


def test_repo_with_no_linked_city_stops(monkeypatch, tmp_path):
    monkeypatch.setattr("robocity_sim.live.git_repo_slug", lambda d: "owner/repo")
    monkeypatch.setattr("robocity_sim.live.slug_for_repo", lambda s, r: None)
    with pytest.raises(WorldUnavailable):
        cli._resolve_world(_args(controller=str(tmp_path / "main.py")))


def test_banner_names_the_world_prominently(capsys):
    cli._print_world_banner(
        {"origin": "city 'my-city' on https://example.test", "seed": 100057250,
         "config": {"starting_fleet": 5}, "start": "fresh world at tick 0"},
        "robot-city", 400)
    out = capsys.readouterr().out
    assert "WORLD : city 'my-city'" in out
    assert "seed  : 100057250" in out
    assert "starting_fleet=5" in out


def test_seed_and_canonical_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "main.py", "--seed", "1", "--canonical"])
