"""``robocity-sim`` command-line entry point.

    robocity-sim run <main.py> [--ticks N] [--seed S | --canonical | --from-live]
                               [--module M] [--json] [--city SLUG] [--server URL]
                               [--skip-code-check]
    robocity-sim check [<main.py>] [--server URL]
    robocity-sim inspect [--state | --logs [N] | --errors [RELEASE]] [--city SLUG] [--server URL]

``run`` drives your controller against the **REAL** Robot City engine — the exact
same binary the server runs, downloaded on demand (and cached) by the vendored
``simcode`` client library. There is no local re-implementation to drift: your ``main.py``
runs against the actual game logic before you push.

WHICH WORLD A RUN USES
----------------------
By default it uses **your city's world**: it resolves this repo -> city slug
(public, no token) and reuses that city's seed *and* its per-city config, then
runs a fresh simulation from tick 0. A cold start stays the default because it is
reproducible and works before you have a city.

``--from-live`` instead starts from **your city as it is now** — its saved world
(buildings, fleet, stored materials, level, and every robot's in-flight command)
plus its saved store, continuing the city's own tick numbering. That is the one
situation a real deploy creates and the only one a cold start cannot reproduce.

If that world cannot be obtained, the run **stops**. It never silently uses a
different one — two identical runs once produced two different worlds, with
different starting fleets and a different quest ladder, and the only sign was one
word inside a normal-looking banner (#73 req 2). To deliberately run a different
world, say so: ``--seed N`` or ``--canonical``. Every run prints which world it
used, in the banner AND in the summary, and ``--json`` carries it in a ``world``
block.

WHAT A PASSING RUN MEANS
------------------------
``run`` first asks the server whether the code would be ACCEPTED on deploy, using
the same rule a real push runs (``POST /api/code/validate``). This tool keeps no
copy of that rule — a copied list is how ``__slots__`` came to pass locally and be
refused on deploy (#73 req 3). ``robocity-sim check`` runs just that step.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# The banner and the summary both name the world; this rule makes the banner hard
# to skim past.
_RULE = "=" * 72


def _project_dir(controller: str) -> str:
    return os.path.dirname(os.path.abspath(controller)) or "."


def _print_world_banner(world: dict, module: str, ticks: int) -> None:
    """State plainly which world this run uses and where it came from (#73 req 2)."""
    lines = [
        _RULE,
        f" WORLD : {world.get('origin')}",
        f" seed  : {world.get('seed')}",
    ]
    cfg = world.get("config")
    if cfg:
        lines.append(" config: " + ", ".join(f"{k}={v}" for k, v in sorted(cfg.items())))
    else:
        lines.append(" config: module defaults (this world carries no per-city config)")
    lines.append(f" engine: {module}   ticks: {ticks}   start: {world.get('start')}")
    if world.get("map_state") is not None:
        lines.append(f" store : {world.get('store_keys', 0)} saved key(s) restored "
                     f"(robot memory starts empty, as it does after a real push)")
        lines += _resume_caveats(world)
    lines.append(_RULE)
    print("\n".join(lines))


def _resume_caveats(world: dict) -> list:
    """The two things a resumed run cannot promise. Said once, plainly.

    Silence is the dangerous option here: a wrong engine restores a partly-zeroed
    world WITHOUT erroring, and a read model seeded from a newer display state
    disagrees with the restored world until the run catches up.
    """
    out = []

    src = world.get("engine_version_source") or "none"
    ver = world.get("engine_version") or ""
    server_ver = world.get("server_engine_version") or "unknown"
    if src == "save" and ver:
        # Saves record the build that wrote them (forum #31), so this is now a real
        # check rather than two version strings printed side by side. Say the
        # VERDICT: a mismatch is the case that silently zeroes part of the world.
        if server_ver != "unknown" and ver == server_ver:
            out.append(f" engine check: OK — this save was produced by engine {ver}, "
                       "the same build you are running.")
        elif server_ver != "unknown":
            out.append(f" engine check: MISMATCH — this save was produced by engine {ver}, "
                       f"but you are running {server_ver}.")
            out.append("               A mismatched engine restores a partly-zeroed world "
                       "WITHOUT any error. Re-download the engine, or run --canonical.")
        else:
            out.append(f" engine check: save was produced by engine {ver}; "
                       "this server publishes no engine version to compare with.")
    else:
        out.append(" engine check: NOT POSSIBLE — this save records no engine version, so I")
        out.append("               cannot verify it matches the engine you are running "
                   f"(server publishes {server_ver}).")
        out.append("               A mismatched engine restores a partly-zeroed world "
                   "WITHOUT any error.")

    save_tick, prime_tick = world.get("save_tick"), world.get("prime_tick")
    if isinstance(save_tick, int) and isinstance(prime_tick, int):
        skew = prime_tick - save_tick
        if skew > 0:
            out.append(f" read model  : seeded from the city's display state at tick {prime_tick},")
            out.append(f"               {skew} tick(s) NEWER than the saved world "
                       f"(tick {save_tick}) the engine")
            out.append("               restored. The engine is authoritative; the two "
                       "converge as the run proceeds.")
    return out


def _resolve_city(args: argparse.Namespace) -> str:
    """The city slug this run is about: --city, else the repo's linked city.

    Raises :class:`live.WorldUnavailable` rather than guessing.
    """
    from .live import WorldUnavailable, git_repo_slug, slug_for_repo

    if args.city:
        return args.city
    repo = git_repo_slug(_project_dir(args.controller))
    if not repo:
        raise WorldUnavailable(
            "this directory is not a git repo with an 'origin' remote, so I "
            "cannot tell which city to run.")
    city = slug_for_repo(args.server, repo)  # raises if it cannot ask
    if not city:
        raise WorldUnavailable(f"no city on {args.server} is linked to {repo}.")
    return city


def _resolve_world(args: argparse.Namespace) -> dict:
    """Work out the world to run, or raise with a clear explanation.

    Exactly one of four sources, always named in the result's ``origin``:
      * ``--seed N``      — an explicit seed you asked for
      * ``--canonical``   — the module's canonical map, asked for explicitly
      * ``--from-live``   — your city AS IT IS NOW: its saved world + saved store,
                            continuing its own tick numbering
      * (default)         — your city's seed + per-city config, fresh from tick 0

    There is no fifth, implicit source. If the requested one cannot be obtained
    this raises; it must never quietly become one of the others.
    """
    from .live import (CANONICAL_SEED, WorldUnavailable, public_snapshot,
                       saved_world_of_city, world_of_city)

    if args.seed is not None:
        return {"seed": args.seed, "city": args.city or "local", "config": None,
                "origin": f"explicit --seed {args.seed}",
                "start": "fresh world at tick 0"}

    if args.canonical:
        return {"seed": CANONICAL_SEED, "city": "local", "config": None,
                "origin": f"the module's canonical map (--canonical), seed {CANONICAL_SEED}",
                "start": "fresh world at tick 0"}

    city = _resolve_city(args)

    if args.from_live:
        doc = saved_world_of_city(args.server, city)  # raises; never falls back
        save = doc["save"]
        save_tick = int(save.get("tick") or 0)
        store = doc.get("store")
        store = store if isinstance(store, dict) else {}

        # The read model must be primed: a restored engine reports only the ticks
        # it runs, so without this the handlers would read a nearly empty world
        # while the engine held the full one. The display snapshot is the same
        # projection a live controller reads — but it is taken at the city's
        # CURRENT tick, which runs ahead of the last checkpoint, so record the
        # skew and let the caller state it.
        try:
            prime = public_snapshot(args.server, city)
        except Exception as exc:
            raise WorldUnavailable(
                f"resumed city '{city}' but could not read its display snapshot "
                f"from {args.server} to seed the read model ({exc}). Refusing to "
                f"run: your handlers would see an almost empty world.") from exc

        cfg = doc.get("config")
        return {
            "seed": doc.get("seed"),
            "city": city,
            "config": cfg if isinstance(cfg, dict) else None,
            "module_type": doc.get("type") or None,
            "origin": f"city '{city}' on {args.server}, AS IT IS NOW (--from-live)",
            "start": f"resumed at tick {save_tick + 1} (saved world from tick {save_tick})",
            "map_state": save,
            "store": store,
            "store_keys": len(store),
            "prime": prime,
            "save_tick": save_tick,
            "prime_tick": prime.get("tick"),
            "engine_version": doc.get("engine_version") or "",
            "engine_version_source": doc.get("engine_version_source") or "none",
            "server_engine_version": doc.get("server_engine_version") or "",
        }

    seed, cfg = world_of_city(args.server, city)  # raises if it cannot read
    return {"seed": seed, "city": city, "config": cfg,
            "origin": f"city '{city}' on {args.server}",
            "start": "fresh world at tick 0"}


def _check_code(args: argparse.Namespace, quiet: bool = False) -> int:
    """Ask the server whether this repo would be accepted on deploy.

    0 = accepted, 4 = rejected, 5 = could not ask. The three are deliberately
    distinct: "your code is bad" and "I could not find out" must not look alike.
    """
    from .live import ValidationUnavailable, collect_sources, validate_sources

    project = _project_dir(args.controller)
    files = collect_sources(project)
    entry = os.path.basename(os.path.abspath(args.controller))
    if entry not in files:  # controller outside the project root; include it anyway
        try:
            with open(args.controller, "r", encoding="utf-8") as fh:
                files[entry] = fh.read()
        except OSError as exc:
            print(f"error: cannot read {args.controller}: {exc}", file=sys.stderr)
            return 2

    try:
        verdict = validate_sources(args.server, "python", files)
    except ValidationUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("       This run would not have told you whether a deploy accepts your "
              "code, so it stops here.\n"
              "       Re-run when the server is reachable, or pass --skip-code-check "
              "to run anyway (a clean run then guarantees nothing about deploying).",
              file=sys.stderr)
        return 5

    if not verdict.get("ok"):
        print("", file=sys.stderr)
        print("CODE-CHECK: REJECTED — a deploy would refuse this repo:", file=sys.stderr)
        print(f"  {verdict.get('error')}", file=sys.stderr)
        print("  (this is the same rule the server runs on push; the release would "
              "never load and your city would keep running the previous code)",
              file=sys.stderr)
        return 4
    if not quiet:
        print(f"CODE-CHECK: OK — {args.server} would accept this repo "
              f"({verdict.get('files')} file(s)).")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Run only the acceptance check — the deploy verdict, without simulating."""
    if not os.path.exists(args.controller):
        print(f"error: no such controller file: {args.controller!r}", file=sys.stderr)
        return 2
    return _check_code(args)


def cmd_run(args: argparse.Namespace) -> int:
    from simcode._local import run_local, _format_summary
    from .live import WorldUnavailable

    if not os.path.exists(args.controller):
        print(f"error: no such controller file: {args.controller!r}", file=sys.stderr)
        return 2

    # 1. Would a deploy accept this code at all? Same rule, asked of the server.
    if not args.skip_code_check:
        rc = _check_code(args, quiet=args.json)
        if rc:
            return rc

    # 2. Which world? Stop rather than substitute (#73 req 2).
    try:
        world = _resolve_world(args)
    except WorldUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("       I will not run a different world instead — the result would "
              "not be about your city.\n"
              "       Choose one explicitly:\n"
              "         robocity-sim run <main.py> --city <slug>   run a specific city's world\n"
              "         robocity-sim run <main.py> --seed <N>      run a specific seed\n"
              "         robocity-sim run <main.py> --canonical     run the module's canonical map",
              file=sys.stderr)
        return 6

    if not args.json:
        _print_world_banner(world, args.module, args.ticks)

    try:
        summary = run_local(args.controller, seed=world["seed"], ticks=args.ticks,
                            module=args.module, city=world["city"],
                            city_config=world["config"],
                            module_type=world.get("module_type"),
                            map_state=world.get("map_state"),
                            initial_store=world.get("store"),
                            prime_state=world.get("prime"),
                            world_source={"origin": world["origin"],
                                          "start": world["start"],
                                          "store_keys": world.get("store_keys")})
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


def _discovered_cells(runs) -> int:
    """Cells covered by the RLE runs [[y, x0, x1], ...] — x0/x1 INCLUSIVE."""
    total = 0
    for run in runs or []:
        if len(run) >= 3:
            total += run[2] - run[1] + 1
    return total


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
        # `discovered` is RLE: [[y, x0, x1], ...] with x0/x1 INCLUSIVE. len() is the
        # number of RUNS, which is not what the field is called and is wildly lower
        # than the truth — one city read 40 when it had discovered 1198 (forum #30).
        "discovered_cells": _discovered_cells(snap.get("discovered", [])),
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
        help="run your controller against the real engine (uses your city's world; no token)")
    run.add_argument("controller", help="path to the controller (main.py)")
    run.add_argument("--ticks", type=int, default=500, help="ticks to simulate (default 500)")
    # Exactly one world source. Naming two is a contradiction, not a preference.
    which = run.add_mutually_exclusive_group()
    which.add_argument("--seed", type=int, default=None,
                       help="run this exact world seed instead of your city's")
    which.add_argument("--canonical", action="store_true",
                       help="run the module's canonical map instead of your city's world "
                            "(use this if you have no city yet)")
    which.add_argument("--from-live", action="store_true", dest="from_live",
                       help="start from your city AS IT IS NOW — its saved world and saved "
                            "store, continuing its own tick numbering — instead of a fresh "
                            "world. This is the situation a real push creates.")
    run.add_argument("--module", default="robot-city",
                     help="game module whose engine to run (default: robot-city)")
    run.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    run.add_argument("--city", default=None,
                     help="city slug whose world to run (default: auto-detected from git remote)")
    run.add_argument("--skip-code-check", action="store_true",
                     help="do not ask the server whether a deploy would accept this code "
                          "(a clean run then guarantees nothing about deploying)")
    run.add_argument("--server", default=None,
                     help="server base URL for engine download + world lookup "
                          "(default: $SIMCODE_SERVER, else the public server)")
    run.set_defaults(func=cmd_run)

    chk = sub.add_parser(
        "check",
        help="ask the server whether a deploy would ACCEPT this code — no simulation")
    chk.add_argument("controller", nargs="?", default="main.py",
                     help="path to the controller (default: main.py)")
    chk.add_argument("--server", default=None,
                     help="server base URL (default: $SIMCODE_SERVER, else the public server)")
    chk.set_defaults(func=cmd_check)

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
