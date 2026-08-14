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
robocity-sim run main.py               # run against the real engine (uses THIS city's world)
robocity-sim run main.py --ticks 300   # shorter horizon
robocity-sim run main.py --json        # machine-readable (parse this)
robocity-sim run main.py --seed 7      # run a specific world seed
robocity-sim run main.py --from-live   # start from your city AS IT IS NOW (what a push meets)
robocity-sim run main.py --canonical   # run the canonical map (use this if you have no city yet)
robocity-sim check main.py             # would a deploy ACCEPT this code? (no simulation)
```

Run it **inside your city repo** and it auto-detects which city this is (via the git
remote) and uses that city's **seed and per-city config** — so the local world matches
your live city — then runs a fresh simulation from tick 0.

### Start from your city as it is now

`--from-live` runs your controller forward from the city's **current state** instead of a
brand-new world: its saved world (buildings, fleet, stored materials, level, and every
robot's in-flight command, target and cargo) plus its **saved store**, continuing the
city's own tick numbering. That is the situation every deploy actually creates — new code
meeting a running city — and the one a cold start cannot reproduce.

A cold start stays the default: it is reproducible and it works before you have a city.

Both halves come along on purpose. The store lives OUTSIDE the world, so resuming the
world alone would give a city that looks right and behaves wrong: a controller that keeps
a claim registry or a version stamp there would start blank and re-do work the real city
has already done. Robot memory is left empty, which is exactly what a real push does.

If the live state cannot be obtained the run **stops** (exit `6`) — a city that has never
checkpointed yet is a refusal, not an empty world to invent.

A resumed run says two things plainly, because silence would be the dangerous option:

* **the engine cannot be verified** — nothing stamps a version into a save today, so the
  tool reports that the check is *not possible* and names the engine the server publishes.
  A mismatched engine restores a partly-zeroed world **without erroring**.
* **the read model is seeded from a slightly newer state** — after a restore the engine
  emits only incremental changes, so the read model your handlers see is seeded from the
  city's display snapshot, taken at the city's current tick. The banner reports the skew in
  ticks and the summary reports any drift between what your handlers saw and the counts the
  engine holds (the engine is authoritative).

### It never runs a world you did not ask for

If your city's world cannot be obtained (server unreachable, no city linked to this
repo, a snapshot with no seed), the run **stops** with exit code `6` and tells you how
to proceed. It does **not** quietly use a different world. That used to happen: a failed
lookup became "seed 7, the canonical map", so two identical runs minutes apart tested two
different worlds — different starting fleets, a different quest ladder — and the only
sign was one word in the banner.

To run a different world, ask for it: `--city <slug>`, `--seed <N>`, or `--canonical`.

Every run prints the world it used in a banner **and** repeats it in the summary next to
the verdict; `--json` carries a `world` block (`seed`, `city`, `origin`, `config`,
`start`) so an automated check can assert which world produced the numbers.

### It accepts exactly what a deploy accepts

Before simulating, `run` asks the server whether a real push would ACCEPT this repo,
using the same rule the server runs on push (`POST /api/code/validate`). This tool keeps
**no copy** of that rule — a copied allow-list is how `__slots__` came to pass locally and
be refused on deploy, where the release never loads and the city silently keeps running
the previous code.

Exit codes: `4` = a deploy would reject this code, `5` = the rule could not be consulted
("I could not find out" is not "accepted"), `6` = the world could not be obtained.
`--skip-code-check` opts out explicitly; a clean run then guarantees nothing about
deploying. `robocity-sim check` runs only this step.

`main.py` is used **unchanged**: it does `from simcode import on, robots, world,
buildings`, registers `@on.idle` etc., and the tool imports it (so
`if __name__ == "__main__": run()` does NOT fire) and drives the loop for you.

## Read the output

The run ends with a **SUMMARY** (your scorecard): the `world` it ran, `ticks run`,
`robots alive`, `robots expired`, `robots destroyed`, `buildings` (+ by type),
`base level`, `handler errors`, `map revealed` (cells discovered), and the
`commands`/`events` seen. `--json` gives the same as a JSON document. The command
**exits non-zero if any handler raised** — watch the exit code / `handler_errors`.

### Expired is NOT destroyed
These are different things and are reported as different figures:

| figure | what happened | what it means |
| --- | --- | --- |
| `robots expired` | flew past its lifespan | **normal.** Inevitable end of life — build replacements. |
| `robots destroyed` | battery hit 0 mid-flight | **a bug in your code.** Cargo lost; recharge earlier / fly shorter hops. |

A long run turning over a hundred robots is a healthy fleet, not a fault. The PASS line
keys on `robots destroyed` only.

### What "good" looks like
- `robots destroyed` should be **0** — a non-zero count means a robot ran its battery
  dry mid-flight (recharge earlier / fly shorter hops). `robots expired` may be any
  number; it is expected.
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
robocity-sim inspect             # this city's status                     (public, no token)
robocity-sim inspect --state     # full current world state               (public, no token)
robocity-sim inspect --logs 100  # recent activity log lines              (public, no token)
robocity-sim inspect --errors    # unhandled exceptions since last release(public, no token)
robocity-sim inspect --errors all  # …across every release
```

`inspect` reads the server's **public REST API** — **no token, no MCP**: status/
`--state` from the city snapshot, `--logs` from `/logs`, `--errors` from
`/exceptions`. The city is auto-detected from this repo's git remote (or pass
`--city <slug>`). `--errors` groups exceptions by type + file:line, each with a
sample traceback and the log lines leading up to it — the first thing to check when
a city looks "frozen" (a raise leaves a robot uncommanded).

## Workflow for iterating on a city controller

1. Edit the city's `main.py`.
2. `robocity-sim run main.py --ticks 500 --json` and read the SUMMARY.
3. If robots stall (no growth), get destroyed, or nothing gets mined/built, adjust the
   strategy and re-run. It's deterministic — same seed reproduces the exact run.
4. Once it behaves, push `main.py` to the city repo.

## Repo layout (for maintainers of THIS tool)

- `simcode/` — the **vendored client client library**, copied verbatim from the platform
  (`clients/python/simcode`). The user's code imports it. **Re-sync it whenever the client library
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
