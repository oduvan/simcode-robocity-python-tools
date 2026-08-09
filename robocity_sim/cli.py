"""``robocity-sim`` command-line entry point.

    robocity-sim run <main.py> [--ticks N] [--seed S] [--module M] [--json]
                               [--city SLUG] [--server URL]
    robocity-sim inspect [--state | --logs [N] | --errors [RELEASE]] [--city SLUG] [--server URL]

``run`` drives your controller against the **REAL** Robot City engine — the exact
same binary the server runs, downloaded on demand (and cached) by the vendored
``simcode`` client library. There is no local re-implementation to drift: your ``main.py``
runs against the actual game logic before you push.

By default it uses **your city's map**: it resolves this repo -> city slug (public,
no token) and reuses that city's world seed, then runs a fresh simulation from
tick 0. Pass ``--seed``/``--module`` to override, or ``--city`` if auto-detect
can't tell which city this is.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def cmd_run(args: argparse.Namespace) -> int:
    from simcode._local import run_local, _format_summary
    from .live import git_repo_slug, slug_for_repo, world_of_city

    if not os.path.exists(args.controller):
        print(f"error: no such controller file: {args.controller!r}", file=sys.stderr)
        return 2

    # Which city is this? Used only to pick the world seed (so the local map matches
    # your live city). Auto-detected from the git remote unless --city is given.
    city = args.city
    if not city and args.seed is None:
        repo = git_repo_slug(os.path.dirname(os.path.abspath(args.controller)) or ".")
        if repo:
            try:
                city = slug_for_repo(args.server, repo)
            except Exception:
                city = None  # offline / server down -> fall back to the canonical seed

    # Pick the seed AND the per-city config: explicit --seed wins; else this
    # city's live values; else the canonical map with module defaults. Config
    # rides along with the seed so the local world matches the real one (#50).
    seed, city_config = args.seed, None
    if city:
        live_seed, city_config = world_of_city(args.server, city)
        if seed is None:
            seed = live_seed
    if seed is None:
        seed = 7  # the module's canonical map

    if not args.json:
        where = f"city '{city}'" if city else "the canonical map"
        if city and city_config:
            where += f" + its config ({', '.join(sorted(city_config))})"
        print(f"running your controller against the REAL {args.module} engine "
              f"(seed {seed}, from {where}, {args.ticks} ticks)…")

    try:
        summary = run_local(args.controller, seed=seed, ticks=args.ticks,
                            module=args.module, city=(city or "local"),
                            city_config=city_config)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # engine download/build failure, import error, …
        print(f"error: local run could not start: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(_format_summary(summary))
    # Non-zero exit when the controller raised, so CI / an AI loop notices.
    return 3 if summary.get("handler_errors", 0) else 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Print a city's live info as JSON, no simulation. Everything comes from the
    server's PUBLIC REST API — no token, no MCP: state/status from the snapshot,
    --logs from /logs, --errors from /exceptions. The city is auto-detected from
    this repo's git remote (or pass --city)."""
    from .live import (git_repo_slug, slug_for_repo, public_snapshot,
                       public_logs, public_exceptions)

    try:
        # Resolve the city — token-free via the public repo->slug lookup.
        city = args.city
        if not city:
            repo = git_repo_slug(os.getcwd())
            if not repo:
                print("error: run this inside your city's git repo, or pass --city <slug>.", file=sys.stderr)
                return 2
            city = slug_for_repo(args.server, repo)
            if not city:
                print(f"error: no city on {args.server} is linked to {repo}.", file=sys.stderr)
                return 2

        if args.errors is not None:  # unhandled exceptions since last release → PUBLIC /exceptions
            doc = public_exceptions(args.server, city, args.errors or None)
        elif args.logs is not None:  # recent logs → PUBLIC /logs
            doc = public_logs(args.server, city, args.logs)
        elif args.state:  # full world state → PUBLIC snapshot
            doc = public_snapshot(args.server, city)
        else:  # default: a compact status derived from the PUBLIC snapshot
            doc = _status_from_snapshot(city, public_snapshot(args.server, city))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(doc, indent=2))
    return 0


def _status_from_snapshot(city: str, snap: dict) -> dict:
    by_type: dict = {}
    for b in snap.get("buildings", []):
        by_type[b.get("type", "?")] = by_type.get(b.get("type", "?"), 0) + 1
    out = {
        "city": city,
        "tick": snap.get("tick"),
        "seed": (snap.get("world") or {}).get("seed"),
        "robots": len(snap.get("robots", [])),
        "buildings": len(snap.get("buildings", [])),
        "buildings_by_type": by_type,
        "discovered_cells": len(snap.get("discovered", [])),
        "stats": snap.get("stats"),
    }
    # Health SIGNAL: unhandled exceptions since your last release. A raise leaves a
    # robot uncommanded, so a "frozen" city is usually this.
    he = snap.get("handler_errors") or 0
    out["handler_errors"] = he
    if he:
        out["hint"] = f"{he} unhandled exception(s) since your last release — run: robocity-sim inspect --errors"
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="robocity-sim",
        description="Local test tool for the SimCode Robot City Builder — runs your "
                    "controller against the real, downloaded game engine.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run",
        help="run your controller against the real engine (uses your city's map; no token)")
    run.add_argument("controller", help="path to the controller (main.py)")
    run.add_argument("--ticks", type=int, default=500, help="ticks to simulate (default 500)")
    run.add_argument("--seed", type=int, default=None,
                     help="world seed (default: your city's seed, else the canonical map)")
    run.add_argument("--module", default="robot-city",
                     help="game module whose engine to run (default: robot-city)")
    run.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    run.add_argument("--city", default=None,
                     help="city slug to borrow the seed from (default: auto-detected from git remote)")
    run.add_argument("--server", default=None,
                     help="server base URL for engine download + seed lookup "
                          "(default: $SIMCODE_SERVER, else the public server)")
    run.set_defaults(func=cmd_run)

    insp = sub.add_parser(
        "inspect",
        help="print your city's live info (state/status/logs/errors) as JSON — public REST, no token, no sim")
    insp.add_argument("--state", action="store_true", help="full current world state (public snapshot)")
    insp.add_argument("--logs", nargs="?", type=int, const=100, default=None,
                      metavar="N", help="recent activity log lines (default 100)")
    insp.add_argument("--errors", nargs="?", const="", default=None, metavar="RELEASE",
                      help="unhandled exceptions since your last release; pass 'all' or a commit SHA to widen")
    insp.add_argument("--city", default=None,
                      help="city slug (default: auto-detected from this repo's git remote)")
    insp.add_argument("--server", default=None,
                      help="server base URL (default: $SIMCODE_SERVER, else the public server)")
    insp.set_defaults(func=cmd_inspect)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # One source of truth for the default server: the client library's env-aware resolver
    # ($SIMCODE_SERVER, else the public default baked into the client library). An explicit
    # --server still wins. This keeps the URL in exactly ONE place.
    if getattr(args, "server", None) is None:
        from simcode._engine_dl import server_base
        args.server = server_base()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
