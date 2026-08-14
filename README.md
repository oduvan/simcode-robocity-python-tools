# simcode-robocity-python-tools

The **local test tool** for the SimCode **Robot City Builder** game. It lets you run
a city controller (`main.py`) on your machine and see what your robots would do —
**before** you push it to your city repo.

`robocity-sim run` drives your controller against the **real game engine**: the exact
same binary the server runs, downloaded on demand and cached. There is **no
re-implementation** to drift and **no parity to maintain** — a local run is the
server's actual game logic.

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

No third-party Python dependencies — standard library only (Python ≥ 3.10). The
first run downloads the engine binary for your OS/arch (a few MB) and caches it
under `~/.cache/simcode/`.

## Run your controller

```bash
# Run against the real engine. Inside your city repo it auto-detects which city
# this is and uses that city's world — seed AND per-city config (public, no token):
robocity-sim run main.py

# Shorter horizon, machine-readable output (for tooling / an AI reading the result):
robocity-sim run main.py --ticks 200 --json

# Run against your city exactly as it is right now (the situation a push creates):
robocity-sim run main.py --from-live

# No city yet? Ask for the canonical map explicitly:
robocity-sim run main.py --canonical

# Would a deploy accept this code? (no simulation)
robocity-sim check main.py
```

Options:

| Flag | Meaning |
| --- | --- |
| `--ticks N` | how many ticks to simulate (default 500) |
| `--seed S` | run this exact world seed instead of your city's |
| `--canonical` | run the module's canonical map instead of your city's world |
| `--from-live` | start from your city AS IT IS NOW (saved world + saved store) |
| `--module M` | game module whose engine to run (default `robot-city`) |
| `--city SLUG` | run this city's world (default: auto-detected from the git remote) |
| `--skip-code-check` | don't ask the server whether a deploy would accept this code |
| `--server URL` | server base URL for engine download + world lookup (default: `$SIMCODE_SERVER`, else the public server) |
| `--json` | emit the summary as JSON instead of the readable block |

**It can start from your city as it is now.** `--from-live` runs your controller forward
from the city's current state — its saved world (buildings, fleet, stored materials, level
and every robot's in-flight command) plus its **saved store**, continuing the city's own
tick numbering. That is the situation a real deploy creates. A cold start stays the
default because it is reproducible and works before you have a city. If the live state
cannot be obtained the run stops; a city that has never checkpointed is a refusal, not an
empty world. A resumed run also states two limits plainly: the save records no engine
version, so that check is *not possible*, and the read model is seeded from the city's
display snapshot, which can be a few ticks newer than the restored save.

**It never runs a world you did not ask for.** If your city's world cannot be obtained
(server unreachable, no linked city), the run stops with exit code `6` and says so — it
does not fall back to another map. Every run prints which world it used, in the banner
and again in the summary; `--json` carries a `world` block.

**It accepts exactly what a deploy accepts.** Before simulating, `run` asks the server
whether a real push would accept this repo, using the same rule the server runs on push.
Exit `4` = would be rejected, `5` = the rule could not be consulted, `6` = the world
could not be obtained.

`main.py` is used **unchanged**: it does `from simcode import on, robots, world,
buildings`, registers `@on.idle` etc., and the tool imports it and drives the tick
loop against the engine for you.

The run ends with a **SUMMARY**: the world it ran, ticks run, robots alive, robots
**expired**, robots **destroyed**, buildings by type, Base level, handler errors, how
much of the map was revealed, and the commands and events seen. It exits non-zero if
any of your handlers raised — so CI or an AI loop notices a broken controller.

**Expired is not destroyed.** `robots expired` = flew past its lifespan: normal,
inevitable end of life, build replacements. `robots destroyed` = battery hit 0
mid-flight: avoidable, and a bug in your code. Only the second one is a problem, and
the PASS line keys on it alone.

### What "good" looks like
- `robots destroyed` should be **0** — a non-zero count means a robot ran its battery
  dry mid-flight (recharge earlier / fly shorter hops). `robots expired` may be any
  number; it is expected on a long run.
- Buildings growing (mining, storage, flying_station, station-produced robots) and the
  Base level climbing means the city is actually developing, not just exploring. The
  shipped starter only explores, so a fresh run shows `buildings: base=1, storage=1`
  and Base level 1 — beat that.

## Inspect a live city without simulating

```bash
robocity-sim inspect                 # compact status of this repo's city (public, no token)
robocity-sim inspect --state         # full current world state           (public, no token)
robocity-sim inspect --logs 100      # recent activity log lines          (public, no token)
robocity-sim inspect --errors        # unhandled exceptions since last release (public, no token)
```

All of `inspect` reads the server's **public REST API** — **no token, no MCP**
(status/`--state` from the snapshot, `--logs` from `/logs`, `--errors` from
`/exceptions`). The city is auto-detected from this repo's git remote (or `--city`).
`--errors` is the first thing to check when a city looks "frozen" — a raised
handler leaves a robot uncommanded.

## How it works

`robocity-sim run` downloads the module's engine (`libengine-<module>-<os>-<arch>`,
the same c-shared library the server runs), loads it via `ctypes`, and drives it one
tick at a time: it feeds the engine your controller's command intents and your active
event subscriptions, gets back the triggered events + a world delta, mirrors the
world exactly like the browser does, and dispatches events through the **unchanged**
vendored `simcode` client library. So the only thing that differs from production is the
transport — the game logic is identical.

Set `SIMCODE_ENGINE_SO=/path/to/libengine-*.so` to run against a local engine build
instead of downloading (used by the smoke test and engine developers).

## License

MIT.
