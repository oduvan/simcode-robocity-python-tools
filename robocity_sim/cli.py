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
import sys
from typing import List

from .config import CANONICAL_SEED
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
    sim = None
    seed = args.seed if args.seed is not None else CANONICAL_SEED
    city = "local"

    if args.from_live:
        if not args.city:
            print("error: --from-live requires --city <slug>", file=sys.stderr)
            return 2
        try:
            from .live import build_sim_from_live
            sim = build_sim_from_live(args.city, server=args.server)
        except Exception as exc:  # network / auth / parse errors
            print(f"error: --from-live failed: {exc}", file=sys.stderr)
            return 1
        city = args.city
        seed = sim.seed
        if not args.quiet and not args.json:
            print(f"[from-live] seeded from {args.city} @ {args.server} "
                  f"(approximate preview) — seed {seed}")

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

    run = sub.add_parser("run", help="run a controller (main.py) locally")
    run.add_argument("controller", help="path to the controller (main.py)")
    run.add_argument("--ticks", type=int, default=500, help="ticks to simulate (default 500)")
    run.add_argument("--seed", type=int, default=None,
                     help=f"world seed (default {CANONICAL_SEED}, the canonical map)")
    run.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    run.add_argument("--quiet", action="store_true",
                     help="suppress the per-tick feed; print only the summary")
    run.add_argument("--from-live", action="store_true",
                     help="seed the world from a live city (approximate preview)")
    run.add_argument("--city", default=None, help="city slug (with --from-live)")
    run.add_argument("--server", default="https://robocity.lyabah.com",
                     help="MCP server base URL (with --from-live)")
    run.set_defaults(func=cmd_run)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
