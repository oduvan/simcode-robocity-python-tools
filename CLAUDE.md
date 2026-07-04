# CLAUDE.md — using this test tool when writing city code

**This repo is a TEST TOOL, not a city.** It is the local, offline simulator for the
SimCode **Robot City Builder** game. If you are an AI writing/iterating on a city
controller (`main.py`), use this to **check your solution locally BEFORE pushing** it
to the city repo. It runs your `main.py` against a faithful Python port of the server
engine — no network, no GitHub, no waiting for a deploy.

## Install it

```bash
pip install "git+https://github.com/oduvan/simcode-robocity-python-tools"
# or, from a checkout:  pip install -e .
```

## Run your controller

**It always tests your code against your city's CURRENT state** — the whole point
is "if I push this *now*, does it work?". A city's live state is **public**, so
this needs **no token**: run it inside your city repo and it auto-detects which
city this is (via the git remote) and fetches that city's current state.

```bash
robocity-sim run main.py               # ← test against this city's CURRENT state (no token)
robocity-sim run main.py --ticks 300   # shorter horizon
robocity-sim run main.py --json        # machine-readable (parse this)
robocity-sim run main.py --city other  # test against a specific city slug
```

The first output line is `[<slug>] testing your code against this city's CURRENT
state`. If it can't resolve a city (not inside the repo, no linked city) it
**stops with a clear error** — it never silently tests a fake empty world. A run
is deliberately **approximate**: tiny randomness/timing differences from the
server are fine — you're checking that your **logic works**, not predicting the
server exactly. Real edge cases surface after you push.

`main.py` is used **unchanged**: it does `from simcode import on, robots, world,
buildings, run`, registers `@on.idle` etc., and the tool imports it (so
`if __name__ == "__main__": run()` does NOT fire) and drives the loop for you.

## Read the output

- **Per-tick feed** (default): each line is `t<tick> <robot> <event>` for game events,
  or `t<tick> <robot>: <text>` for your `r.log(...)` lines. This is your trace of
  what the fleet actually did.
- **SUMMARY** (always, at the end): `final tick`, `robots`, `robots destroyed`,
  `buildings` (+ by type), `ore`/`metal` **mined / stored**, `spots found`,
  `discovered cells`. This is your scorecard.
- `--json` gives `{seed, ticks, city, summary, feed[]}` — parse `summary` to grade a
  run and `feed` to see the sequence of events.

### What "good" looks like
- `robots destroyed` should be **0** — a non-zero count means a robot ran its battery
  dry mid-flight (recharge earlier / fly shorter hops).
- `ore.mined` / `metal.mined` climbing and `buildings_by_type` growing (mining,
  storage, flying_station, more base-produced robots) means the city is actually
  developing, not just exploring. The shipped starter only explores, so a fresh run
  of it shows `mined: 0` and `buildings: base=1` — beat that.

## Inspect your city without simulating

Sometimes you just want to *see* your city's live data — not run your code (JSON out):

```bash
robocity-sim inspect             # this city's status         (public, no token)
robocity-sim inspect --state     # full current world state   (public, no token)
robocity-sim inspect --logs 100  # recent activity log lines  (needs SIMCODE_TOKEN)
robocity-sim inspect --list      # all your cities            (needs SIMCODE_TOKEN)
```

`inspect` and `--state` read the **public** city snapshot (no token). `--logs` and
`--list` aren't public, so they use the authed MCP tools (`get_recent_logs` /
`list_cities`) and need `SIMCODE_TOKEN`.

## Important: it's a faithful PREVIEW, not the server

- The engine here is a **re-implementation** of the server's Go engine. World
  generation is **verified byte-identical** (same seed → same map, spot positions and
  richness), and the rules/events/timing mirror the server (intents lag one tick, just
  like production). Parity is maintained against the Go source; if you find a
  divergence in mechanics, treat it as a bug in this tool.
- It tests against your city's current **public** snapshot (robots, buildings,
  inventories, energy) plus the full map regenerated from the seed. A run is an
  **approximate** continuation — small randomness/timing differences from the
  server are expected and fine. You're checking that your **logic runs correctly**
  (no crashes, sensible behavior), not forecasting the server bit-for-bit; real
  edge cases surface after you push.

## Handler errors & subscription fidelity

- **Crashes are surfaced, not swallowed.** If a handler raises on an event, the
  run continues (one bad event can't kill the loop, exactly like the server) but
  the tool **reports it**: a `⚠ N handler error(s)` block on stderr, a
  `handler errors` line in the SUMMARY, an `errors[]` array in `--json`, and a
  **non-zero exit code**. So a bug in your controller shows up here instead of
  after a push. (Watch the exit code / the `handler_errors` count in a loop.)
- **Subscriptions behave like the server** for the normal pattern (handlers
  registered at import via `@on.idle` etc.), including `once` and `idle`
  re-emission (a passive handler keeps getting events; robots never permanently
  stall). The ONLY server behavior not reproduced: the *instantaneous replay* the
  server sends when a handler subscribes to `spawn`/`idle` **mid-run** — here that
  handler instead receives the next emission a few ticks later. Equivalent for
  virtually every controller.

## Workflow for iterating on a city controller

1. Edit the city's `main.py`.
2. `robocity-sim run main.py --ticks 500 --json` and read the `summary` + tail of
   `feed`.
3. If robots stall (no growth), get destroyed, or nothing gets mined/built, adjust the
   strategy and re-run. It's deterministic — same seed reproduces the exact run, so a
   change's effect is directly comparable.
4. Once it behaves, push `main.py` to the city repo.

## Repo layout (for maintainers of THIS tool)

- `simcode/` — the **vendored client SDK**, copied verbatim from the platform. Do not
  change its client API; the user's code imports it. Re-sync from
  `sdk/python/simcode` when the SDK changes.
- `robocity_sim/` — the ported engine + driver + CLI:
  - `world.py` — endless world, `hash_cell` (SplitMix64, masked to 64-bit), generation.
  - `module.py` — the rules: Submit/Advance, commands, autonomous mining/construction,
    Base production, events, and the `state.*` snapshot the SDK reads.
  - `fakeredis.py` — in-process fake Redis (state KV + captured pub/sub + streams).
  - `driver.py` — the tick loop (mirrors the Go engine `step`); wires SDK ↔ engine.
  - `cli.py` — `robocity-sim` entry point. `live.py` — `--from-live`.
- Parity is guarded by porting the Go source under `game/modules/robot_city`. When the
  Go engine changes, update `robocity_sim` and the vendored `simcode/` together.

## Test this tool

Per the platform's Docker-only rule:

```bash
docker run --rm -v "$PWD":/app -w /app python:3.13-slim \
  sh -c "pip install -q -e . && pip install -q pytest && python -m pytest -q \
         && robocity-sim run examples/starter_main.py --ticks 300"
```

Tests cover: determinism (same seed → identical feed/summary), `hash_cell`/spot-field
stability for seed 7, a starter run (robots move, energy drains + recharges, map
grows), and autonomous mining (fills at MiningSpeed/tick, caps at MiningStorageCap).
