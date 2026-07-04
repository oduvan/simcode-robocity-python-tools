"""``robocity-sim`` command-line entry point.

    robocity-sim run <main.py> [--ticks N] [--seed S] [--json] [--quiet]
                               [--from-live --city SLUG [--server URL]]

Default is a FRESH run: tick 0, embedded canonical config, no network. It streams
the per-tick activity feed (game events + your ``r.log()`` lines) and prints a
SUMMARY at the end.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

from .driver import run_simulation, Simulation
from .module import FeedEvent


def _human_stream(tick: int, feed: List[FeedEvent]) -> None:
    for f in feed:
        print(f.line())


def _print_summary(summary: dict) -> None:
    by_type = summary.get("buildings_by_type", {})
    ore = summary.get("ore", {})
    metal = summary.get("metal", {})
    print("")
    print("=" * 48)
    print("SUMMARY")
    print("=" * 48)
    print(f"  final tick        : {summary.get('final_tick')}")
    print(f"  robots            : {summary.get('robots')}")
    print(f"  robots destroyed  : {summary.get('robots_destroyed')}")
    print(f"  buildings         : {summary.get('buildings')}")
    if by_type:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
        print(f"    by type         : {parts}")
    print(f"  ore   (mined/stored): {ore.get('mined', 0)} / {ore.get('stored', 0)}")
    print(f"  metal (mined/stored): {metal.get('mined', 0)} / {metal.get('stored', 0)}")
    print(f"  spots found       : {summary.get('spots_found')}")
    print(f"  discovered cells  : {summary.get('discovered_cells')}")
    errs = summary.get("handler_errors", 0)
    if errs:
        print(f"  handler errors    : {errs}  <-- your controller raised (see above)")


def cmd_run(args: argparse.Namespace) -> int:
    # The tool always tests your code against your city's CURRENT state — "would
    # this work if I deployed it right now?". It fetches the live world for this
    # city (auto-detected from the repo's git remote, or --city) and runs your
    # code against it. It can't do that without a token + a resolvable city, so
    # those are hard errors (no silent fall-back to a fake empty world).
    from .live import build_sim_from_live, git_repo_slug, detect_city

    token = os.environ.get("SIMCODE_TOKEN")
    if not token:
        print('error: set SIMCODE_TOKEN first (dashboard → "Connect via MCP"). '
              "The tool tests your code against your city's current state, so it needs your token.",
              file=sys.stderr)
        return 2

    city = args.city
    if not city:
        repo = git_repo_slug(os.path.dirname(os.path.abspath(args.controller)))
        if not repo:
            print("error: run this inside your city's git repo (so I can tell which city it is), "
                  "or pass --city <slug>.", file=sys.stderr)
            return 2
        try:
            city = detect_city(args.server, token, repo)
        except Exception as exc:
            print(f"error: couldn't list your cities: {exc}", file=sys.stderr)
            return 1
        if not city:
            print(f"error: no city on {args.server} is linked to {repo}. "
                  "Create/link a city first, or pass --city <slug>.", file=sys.stderr)
            return 2

    try:
        sim = build_sim_from_live(city, server=args.server, token=token)
    except Exception as exc:  # network / auth / parse errors
        print(f"error: couldn't fetch '{city}' state: {exc}", file=sys.stderr)
        return 1
    seed = sim.seed
    if not args.json:
        print(f"[{city}] testing your code against this city's CURRENT state")

    on_tick = None
    if not args.quiet and not args.json:
        on_tick = _human_stream

    try:
        result = run_simulation(
            args.controller, ticks=args.ticks, seed=seed, city=city, sim=sim,
            on_tick=on_tick,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    errors = [
        {
            "event": e.get("event"),
            "robot": e.get("robot"),
            "handler": e.get("handler"),
            "error": (e.get("error") or "").strip().splitlines()[-1] if e.get("error") else "",
            "traceback": e.get("error", ""),
        }
        for e in result.errors
    ]

    if args.json:
        out = {
            "seed": result.seed,
            "ticks": result.ticks,
            "city": result.city,
            "summary": result.summary,
            "errors": errors,
            "feed": [{"tick": t, "line": line} for t, line in result.feed],
        }
        print(json.dumps(out, indent=2))
    else:
        if errors:
            # Your controller crashed on some events. The SDK isolates handler
            # exceptions (so one bad event doesn't kill the run) — surfaced here
            # so you actually SEE the bug locally instead of after a push.
            print("", file=sys.stderr)
            print(f"⚠ {len(errors)} handler error(s) — your controller raised:", file=sys.stderr)
            for e in errors[:5]:
                where = f"{e['handler']} on '{e['event']}'" + (f" (robot {e['robot']})" if e["robot"] else "")
                print(f"  - {where}: {e['error']}", file=sys.stderr)
            if len(errors) > 5:
                print(f"  … and {len(errors) - 5} more (use --json for full tracebacks)", file=sys.stderr)
        _print_summary(result.summary)
    # Non-zero exit when the controller raised, so CI / an AI loop notices.
    return 3 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="robocity-sim",
        description="Local offline simulator for the SimCode Robot City Builder game.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run",
        help="run your controller against your city's CURRENT state (needs SIMCODE_TOKEN)")
    run.add_argument("controller", help="path to the controller (main.py)")
    run.add_argument("--ticks", type=int, default=500, help="ticks to simulate (default 500)")
    run.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    run.add_argument("--quiet", action="store_true",
                     help="suppress the per-tick feed; print only the summary")
    run.add_argument("--city", default=None,
                     help="city slug to test against (default: auto-detected from this repo's git remote)")
    run.add_argument("--server", default="https://robocity.lyabah.com",
                     help="MCP server base URL")
    run.set_defaults(func=cmd_run)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
