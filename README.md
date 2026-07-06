# simcode-robocity-python-tools

A **local, offline simulator** for the SimCode **Robot City Builder** game. It lets
you test a city controller (`main.py`) on your machine — **no GitHub push, no
network, no server** — and see what your robots would do.

It re-implements the server's game engine in Python and drives the **unchanged**
`simcode` client SDK, so your `main.py` runs byte-for-byte the same as it does on
the server. The world generation is a faithful port of the Go engine and is
**verified identical** (same seed 7 → same map, same spot positions/richness).

> This is a **test tool**, not the platform and not your city repo. Your controller
> still ships by pushing to your city repo; this just lets you check it first.

## Install

```bash
pip install "git+https://github.com/oduvan/simcode-robocity-python-tools"
```

or from a checkout:

```bash
pip install -e .
```

No third-party dependencies — standard library only (Python ≥ 3.10).

## Usage

```bash
# Fresh canonical run (seed 7 — the same map every city of this type starts from):
robocity-sim run main.py

# Shorter run, only the summary:
robocity-sim run main.py --ticks 200 --quiet

# Machine-readable output (for tooling / an AI reading the result):
robocity-sim run main.py --ticks 500 --json
```

Options:

| Flag | Meaning |
| --- | --- |
| `--ticks N` | how many ticks to simulate (default 500) |
| `--seed S` | world seed (default 7 — the canonical shared map) |
| `--json` | emit a JSON document (`summary` + full `feed`) instead of text |
| `--quiet` | suppress the per-tick feed; print only the summary |

The default output streams the per-tick **activity feed** (game events + your
`r.log(...)` lines, tick-stamped) and ends with a **SUMMARY**: final tick, robot
count, buildings by type, ore/metal mined+stored, discovered-cell count, and how
many robots were destroyed.

### Preview from a live city (`--from-live`)

Seed the local run from a city's *current* world instead of a fresh start:

```bash
export SIMCODE_TOKEN=...        # your MCP bearer token
robocity-sim run main.py --from-live --city my-city-slug
# optional: --server https://robocity.lyabah.com  (default)
```

This fetches the city's public world snapshot over the MCP endpoint
(`POST {server}/mcp`, JSON-RPC `tools/call` → `get_world_state`) and rebuilds an
**approximate** world from it. Because the public snapshot is a lossy, fog-limited
view (no hidden spot richness, no in-flight command internals), a `--from-live`
run is a **rough preview** of "where my city is now", not an exact continuation.
If `SIMCODE_TOKEN` is unset you get a clear error.

## What it models

Everything the reference module does, ported faithfully and deterministically:

- endless, continuous world with lazy **hash-based** cell generation (fog of war),
- **flying** robots with float positions and **energy** (drain on flight,
  destruction mid-flight when the battery hits 0, recharge on a Flying Station /
  the Base),
- **autonomous mining** into capped storage, **self-completing** construction
  sites (`world.build`), and **Flying Station robot production**,
- the full event set (`spawn`, `idle`, `arrived`, `blocked`, `construction_*`,
  `resource_delivered`, `spot_depleted`, `storage_full`, `inventory_full`,
  `robot_produced`, `robot_destroyed`, `charge_complete`, `message`,
  `base_level_up`, `quest_updated`), delivered to
  your handlers exactly as on the server (intents lag one tick, same as prod).

## Determinism

Running the same controller with the same seed twice produces **identical** output
(event feed and summary). The engine has no wall-clock or RNG in its hot path.

## License

MIT.
