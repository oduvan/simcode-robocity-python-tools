"""#73 req 3: the local tool consults the SERVER's acceptance rule, not its own.

Forum post 18: a class using ``__slots__`` passed ``robocity-sim run`` and was
then refused on deploy ("access to forbidden attribute '__slots__'"). The release
never loaded, the city kept running the previous code, and the city page showed
nothing wrong.

The fix is not a second copy of the allow-list here — that is how the drift
happened. The tool posts its sources to ``/api/code/validate`` and reports the
server's verdict, so there is exactly one rule.
"""

import argparse

import pytest

from robocity_sim import cli, live


def _args(tmp_path, **kw):
    base = dict(controller=str(tmp_path / "main.py"), ticks=10, seed=None,
                canonical=False, from_live=False, module="robot-city", json=False, city=None,
                skip_code_check=False, server="https://example.test")
    base.update(kw)
    return argparse.Namespace(**base)


def test_collect_sources_gathers_the_repo_and_skips_vcs(tmp_path):
    (tmp_path / "main.py").write_text("from simcode import on\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "plan.py").write_text("X = 1\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("junk\n")

    files = live.collect_sources(str(tmp_path))

    assert set(files) == {"main.py", "lib/plan.py"}


def test_rejection_is_reported_and_fails_the_run(tmp_path, monkeypatch, capsys):
    (tmp_path / "main.py").write_text("class S:\n    __slots__ = ('a',)\n")
    monkeypatch.setattr(cli, "_check_code", cli._check_code)  # keep the real one
    monkeypatch.setattr("robocity_sim.live.validate_sources", lambda s, l, f: {
        "ok": False, "error": "main.py:2: access to forbidden attribute '__slots__'",
        "file": "main.py", "line": 2,
    })

    rc = cli.cmd_check(_args(tmp_path))

    assert rc == 4
    err = capsys.readouterr().err
    assert "CODE-CHECK: REJECTED" in err
    assert "__slots__" in err


def test_acceptance_passes(tmp_path, monkeypatch, capsys):
    (tmp_path / "main.py").write_text("from simcode import on\n")
    monkeypatch.setattr("robocity_sim.live.validate_sources",
                        lambda s, l, f: {"ok": True, "files": len(f)})

    assert cli.cmd_check(_args(tmp_path)) == 0
    assert "CODE-CHECK: OK" in capsys.readouterr().out


def test_unreachable_validator_is_not_the_same_as_acceptance(tmp_path, monkeypatch, capsys):
    """'I could not find out' must never look like 'accepted'."""
    (tmp_path / "main.py").write_text("from simcode import on\n")

    def boom(server, language, files):
        raise live.ValidationUnavailable("could not reach https://example.test")
    monkeypatch.setattr("robocity_sim.live.validate_sources", boom)

    rc = cli.cmd_check(_args(tmp_path))

    assert rc == 5 and rc != 0 and rc != 4
    err = capsys.readouterr().err
    assert "--skip-code-check" in err  # the explicit, opt-in way to proceed anyway


def test_run_stops_before_simulating_when_the_code_would_be_rejected(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("import os\n")
    monkeypatch.setattr("robocity_sim.live.validate_sources", lambda s, l, f: {
        "ok": False, "error": "main.py:1: import of forbidden module 'os'"})
    # If the run got past the check it would try to resolve a world and download
    # an engine; both would fail loudly rather than silently pass this test.
    assert cli.cmd_run(_args(tmp_path)) == 4
