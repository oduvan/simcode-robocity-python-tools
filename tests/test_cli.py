"""CLI wiring tests for ``robocity-sim`` (no engine / no network)."""

from robocity_sim.cli import build_parser, cmd_run


def test_parser_run_defaults():
    args = build_parser().parse_args(["run", "main.py"])
    assert args.command == "run"
    assert args.controller == "main.py"
    assert args.ticks == 500
    assert args.seed is None
    assert args.module == "robot-city"


def test_parser_inspect_modes():
    args = build_parser().parse_args(["inspect", "--state", "--city", "my-city"])
    assert args.command == "inspect"
    assert args.state is True
    assert args.city == "my-city"


def test_run_missing_controller_exits_2(tmp_path):
    args = build_parser().parse_args(["run", str(tmp_path / "nope.py")])
    # No network / engine touched: it bails on the missing file first.
    assert cmd_run(args) == 2
