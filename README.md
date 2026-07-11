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
# this is and borrows that city's map seed (public, no token needed):
robocity-sim run main.py

# Shorter horizon, machine-readable output (for tooling / an AI reading the result):
robocity-sim run main.py --ticks 200 --json
```

Options:

| Flag | Meaning |
| --- | --- |
| `--ticks N` | how many ticks to simulate (default 500) |
| `--seed S` | world seed (default: your city's seed, else the canonical map, 7) |
| `--module M` | game module whose engine to run (default `robot-city`) |
| `--city SLUG` | borrow the seed from this city (default: auto-detected from the git remote) |
| `--server URL` | server base URL for engine download + seed lookup (default `https://robocity.lyabah.com`) |
| `--json` | emit the summary as JSON instead of the readable block |

`main.py` is used **unchanged**: it does `from simcode import on, robots, world,
buildings`, registers `@on.idle` etc., and the tool imports it and drives the tick
loop against the engine for you.

The run ends with a **SUMMARY**: ticks run, robots alive/destroyed, buildings by
type, Base level, handler errors, how much of the map was revealed, and the commands
and events seen. It exits non-zero if any of your handlers raised — so CI or an AI
loop notices a broken controller.

### What "good" looks like
- `robots destroyed` should be **0** — a non-zero count means a robot ran its battery
  dry mid-flight (recharge earlier / fly shorter hops).
- Buildings growing (mining, storage, flying_station, station-produced robots) and the
  Base level climbing means the city is actually developing, not just exploring. The
  shipped starter only explores, so a fresh run shows `buildings: base=1, storage=1`
  and Base level 1 — beat that.

## Inspect a live city without simulating

```bash
robocity-sim inspect                 # compact status of this repo's city (public, no token)
robocity-sim inspect --state         # full current world state (public, no token)
robocity-sim inspect --logs 100      # recent activity log lines   (needs SIMCODE_TOKEN)
robocity-sim inspect --list          # list your cities            (needs SIMCODE_TOKEN)
```

`--state`/status come from the city's **public** snapshot (no token). `--logs` and
`--list` use the authenticated MCP tools and need `SIMCODE_TOKEN`.

## How it works

`robocity-sim run` downloads the module's engine (`libengine-<module>-<os>-<arch>`,
the same c-shared library the server runs), loads it via `ctypes`, and drives it one
tick at a time: it feeds the engine your controller's command intents and your active
event subscriptions, gets back the triggered events + a world delta, mirrors the
world exactly like the browser does, and dispatches events through the **unchanged**
vendored `simcode` SDK. So the only thing that differs from production is the
transport — the game logic is identical.

Set `SIMCODE_ENGINE_SO=/path/to/libengine-*.so` to run against a local engine build
instead of downloading (used by the smoke test and engine developers).

## License

MIT.
