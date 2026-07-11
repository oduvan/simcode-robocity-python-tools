"""Unit tests for the stdlib-only helpers in ``robocity_sim.live``."""

from robocity_sim.live import parse_repo_slug


def test_parse_repo_slug_forms():
    assert parse_repo_slug("git@github.com:owner/repo.git") == "owner/repo"
    assert parse_repo_slug("https://github.com/owner/repo") == "owner/repo"
    assert parse_repo_slug("https://github.com/owner/repo.git") == "owner/repo"
    assert parse_repo_slug("https://github.com/owner/repo/") == "owner/repo"


def test_parse_repo_slug_bad_input():
    assert parse_repo_slug("") is None
    assert parse_repo_slug("nonsense") is None
