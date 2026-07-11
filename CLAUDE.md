# CLAUDE.md — using this test tool when writing city code

**This repo is a TEST TOOL, not a city.** It is the local test runner for the SimCode
**Robot City Builder** game. If you are an AI writing/iterating on a city controller
(`main.py`), use this to **check your solution locally BEFORE pushing** it to the city
repo. It runs your `main.py` against the **real** game engine — the exact same binary
the server runs, downloaded on demand — so there is no re-implementation to drift and
no network/GitHub/deploy wait.

## Install it

```bash
pip install "git+https://github.com/oduvan/simcode-robocity-python-tools"
# or, from a checkout:  pip install -e .
```

The first run downloads the engine for your OS/arch (a few MB) and caches it under
`~/.cache/simcode/`. No third-party Python deps — stdlib only.

## Run your controller

```bash
robocity-sim run main.py               # run against the real engine (uses this city's map seed)
robocity-sim run main.py --ticks 300   # shorter horizon
robocity-sim run main.py --json        # machine-readable (parse this)
robocity-sim run main.py --seed 7      # force a specific world seed
```

Run it **inside your city repo** and it auto-detects which city this is (via the git
remote) and borrows that city's **map seed** — so the local world matches your live
city's map — then runs a fresh simulation from tick 0. If it can't resolve a city (not
inside the repo, offline), it falls back to the **canonical map** (seed 7). Pass
`--seed`/`--city` to control this explicitly.

`main.py` is used **unchanged**: it does `from simcode import on, robots, world,
buildings`, registers `@on.idle` etc., and the tool imports it (so
`if __name__ == "__main__": run()` does NOT fire) and drives the loop for you.

## Read the output

The run ends with a **SUMMARY** (your scorecard): `ticks run`, `robots alive`,
`robots destroyed`, `buildings` (+ by type), `base level`, `handler errors`,
`map revealed` (cells discovered), and the `commands`/`events` seen. `--json` gives
the same as a JSON document. The command **exits non-zero if any handler raised** —
watch the exit code / `handler_errors` in a loop.

### What "good" looks like
- `robots destroyed` should be **0** — a non-zero count means a robot ran its battery
  dry mid-flight (recharge earlier / fly shorter hops).
- Buildings growing (mining, storage, flying_station, station-produced robots) and the
  Base level climbing means the city is actually developing, not just exploring. The
  shipped starter only explores, so a fresh run shows `buildings: base=1, storage=1`
  and Base level 1 — beat that.

## It's the real engine (not a preview)

The game logic is the server's actual engine, so a local run is **not** an
approximation of the rules — same seed → same world, same mechanics, same event
timing (intents lag one tick, exactly like production). The only thing that differs
from production is the transport. Two caveats:

- A run starts from a **fresh tick-0 world** on your city's seed, not your city's
  *current* live state — so it shows what your controller does from the beginning, not
  a continuation of your running city.
- **Crashes are surfaced, not swallowed.** If a handler raises, the run continues (one
  bad event can't kill the loop, like the server) but the tool reports it in the
  SUMMARY (`handler errors`) and via a non-zero exit code.

## Inspect your city without simulating

```bash
robocity-sim inspect             # this city's status         (public, no token)
robocity-sim inspect --state     # full current world state   (public, no token)
robocity-sim inspect --logs 100  # recent activity log lines  (needs SIMCODE_TOKEN)
robocity-sim inspect --list      # all your cities            (needs SIMCODE_TOKEN)
```

`inspect` and `--state` read the **public** city snapshot (no token). `--logs` and
`--list` use the authed MCP tools (`get_recent_logs` / `list_cities`) and need
`SIMCODE_TOKEN`.

## Workflow for iterating on a city controller

1. Edit the city's `main.py`.
2. `robocity-sim run main.py --ticks 500 --json` and read the SUMMARY.
3. If robots stall (no growth), get destroyed, or nothing gets mined/built, adjust the
   strategy and re-run. It's deterministic — same seed reproduces the exact run.
4. Once it behaves, push `main.py` to the city repo.

## Repo layout (for maintainers of THIS tool)

- `simcode/` — the **vendored client SDK**, copied verbatim from the platform
  (`sdk/python/simcode`). The user's code imports it. **Re-sync it whenever the SDK
  changes** — this is how the real-engine runner (`simcode/_local.py`,
  `simcode/_engine_dl.py`) reaches users.
- `robocity_sim/` — the thin CLI (no engine of its own anymore):
  - `cli.py` — the `robocity-sim` `run`/`inspect` entry point.
  - `live.py` — stdlib-only helpers to reach the live server (repo→slug, seed lookup,
    public snapshot, MCP tools).
- The engine itself is **not in this repo** — `run` downloads the real
  `libengine-robot-city-<os>-<arch>` and drives it via `simcode._local`. So there is
  **no parity to maintain**: a mechanics change on the server reaches this tool the
  moment the new engine is published, with no port needed here.

## Test this tool

Per the platform's Docker-only rule (the real-engine smoke test runs only with a local
engine build via `SIMCODE_ENGINE_SO`; without it, the CLI + helper tests still run):

```bash
docker run --rm -v "$PWD":/app -w /app python:3.14-slim \
  sh -c "pip install -q -e . pytest && python -m pytest -q"
```
