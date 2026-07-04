"""``--from-live``: seed a local run from a city's current world.

Best-effort PREVIEW only. It fetches the city's public world snapshot over the
MCP endpoint and rebuilds an approximate world from it. The public snapshot is a
lossy view (fog-of-war, no hidden spot richness, no in-flight command internals),
so a from-live run diverges from the server faster than a fresh canonical run —
treat it as "roughly where my city is now", not an exact continuation.

Uses only the stdlib (urllib) to avoid extra dependencies.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
import urllib.error
from typing import Optional

from .config import Config, default_config, CANONICAL_SEED
from .driver import Simulation
from .module import Module, STATUS_ACTIVE
from .world import Robot, Building, Construction, Spot

DEFAULT_SERVER = "https://robocity.lyabah.com"


def _mcp_call(server: str, token: str, name: str, arguments: dict) -> dict:
    url = server.rstrip("/") + "/mcp"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def _extract_world_state(rpc: dict) -> dict:
    """Pull the world-state document out of an MCP tools/call result.

    get_world_state wraps the module's world doc with a little city context:
    ``{slug, type, deploy_status, state: {tick, world, robots, buildings, tiles,
    discovered, stats}}``. The actual snapshot is under ``state``.
    """
    if rpc.get("error"):
        raise ValueError(f"MCP error: {rpc['error']}")
    result = rpc.get("result", rpc)
    # MCP returns content as a list of {type:"text", text:"...json..."} blocks.
    doc = None
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    doc = json.loads(block["text"])
                    break
                except Exception:
                    continue
    if doc is None and isinstance(result, dict):
        doc = result
    if isinstance(doc, dict):
        # Unwrap the relayDoc envelope, then accept a bare snapshot too.
        if isinstance(doc.get("state"), dict) and "world" in doc["state"]:
            return doc["state"]
        if "world" in doc and ("robots" in doc or "buildings" in doc):
            return doc
        note = doc.get("note")
        if note:  # e.g. "this city has no live state yet (still starting/paused)"
            raise ValueError(f"no usable world state for this city: {note}")
    raise ValueError("could not parse world state from MCP response")


def parse_repo_slug(url: str) -> Optional[str]:
    """`git@github.com:owner/repo.git` / `https://github.com/owner/repo(.git)` -> `owner/repo`."""
    if not url:
        return None
    url = url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@") and ":" in url:
        path = url.split(":", 1)[1]
    else:
        path = url.split("://", 1)[-1]
        path = path.split("/", 1)[1] if "/" in path else path
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def git_repo_slug(directory: str) -> Optional[str]:
    """The `owner/repo` of the git remote in `directory`, or None (not a repo / no remote)."""
    try:
        out = subprocess.run(
            ["git", "-C", directory or ".", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return parse_repo_slug(out.stdout)


def detect_city(server: str, token: str, repo: str) -> Optional[str]:
    """Ask the server (list_cities) for the city slug linked to `repo`, or None."""
    rpc = _mcp_call(server, token, "list_cities", {})
    result = rpc.get("result", rpc)
    doc = None
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    doc = json.loads(block["text"])
                    break
                except Exception:
                    continue
    cities = (doc or {}).get("cities", []) if isinstance(doc, dict) else []
    for c in cities:
        if (c.get("repo") or "").lower() == repo.lower():
            return c.get("slug")
    return None


def mcp_doc(server: str, token: str, name: str, arguments: dict):
    """Call an MCP tool and return its parsed document (the text content block)."""
    rpc = _mcp_call(server, token, name, arguments)
    if rpc.get("error"):
        raise ValueError(f"MCP error: {rpc['error']}")
    result = rpc.get("result", rpc)
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except Exception:
                    return block["text"]
    return result


def build_sim_from_live(city_slug: str,
                        server: str = DEFAULT_SERVER,
                        token: Optional[str] = None,
                        cfg: Optional[Config] = None) -> Simulation:
    """Fetch the live city and build an approximate Simulation seeded from it."""
    token = token or os.environ.get("SIMCODE_TOKEN")
    if not token:
        raise RuntimeError(
            "SIMCODE_TOKEN is not set. Export a bearer token for the MCP server:\n"
            "    export SIMCODE_TOKEN=...   # then re-run --from-live"
        )
    cfg = cfg or default_config()

    rpc = _mcp_call(server, token, "get_world_state", {"city": city_slug})
    snap = _extract_world_state(rpc)

    seed = int(snap.get("world", {}).get("seed", CANONICAL_SEED))
    sim = Simulation(city=city_slug, cfg=cfg, seed=seed)
    _seed_world_from_snapshot(sim.mod, snap)
    return sim


def _seed_world_from_snapshot(mod: Module, snap: dict) -> None:
    """Overlay the fetched snapshot onto a fresh canonical world.

    We start from the deterministic canonical world (same seed) so hidden cells
    stay consistent, then overwrite the discovered tiles, robots and buildings
    with the observed state. Approximate by construction (see module docstring).
    """
    wd = mod.wd
    # Reset the dynamic entities; keep the lazily-generated cell field.
    wd.robots.clear()
    wd.robot_ord.clear()
    wd.buildings.clear()
    wd.build_ord.clear()
    wd.pending_spawn.clear()

    # Tiles / spots (mark discovered + set spot richness where visible).
    for t in snap.get("tiles", []):
        x, y = int(t["x"]), int(t["y"])
        cl = wd.cell_at(x, y)
        sp = t.get("spot")
        if sp:
            cl.spot = Spot(resource=sp.get("resource", "ore"),
                           remaining=int(sp.get("remaining", 0)))
        else:
            cl.spot = None
        wd.discovered[(x, y)] = True
        wd.grow_bounds(x, y)
    for c in snap.get("discovered", []):
        x, y = int(c[0]), int(c[1])
        wd.cell_at(x, y)
        wd.discovered[(x, y)] = True
        wd.grow_bounds(x, y)

    # Buildings.
    for b in snap.get("buildings", []):
        pos = b.get("pos", [0, 0])
        nb = Building(id=b["id"], typ=b.get("type", "storage"),
                      pos=(int(pos[0]), int(pos[1])),
                      status=b.get("status", STATUS_ACTIVE))
        storage = b.get("storage")
        if storage:
            nb.has_storage = True
            nb.ore = int(storage.get("ore", 0))
            nb.metal = int(storage.get("metal", 0))
            nb.cap = int(storage.get("capacity", 0))
        if nb.typ == "mining":
            nb.spot_cell = (nb.pos[0], nb.pos[1])
        cons = b.get("construction")
        if cons and nb.status == "constructing":
            req = cons.get("required", {})
            deliv = cons.get("delivered", {})
            nb.cons = Construction(target_type=nb.typ,
                                   req_ore=int(req.get("ore", 0)),
                                   req_metal=int(req.get("metal", 0)),
                                   build_ticks=1)
            nb.cons.got_ore = int(deliv.get("ore", 0))
            nb.cons.got_metal = int(deliv.get("metal", 0))
            nb.cons.progress = float(cons.get("progress", 0.0))
        wd.add_building(nb)

    # Robots.
    for r in snap.get("robots", []):
        pos = r.get("pos", [0, 0])
        inv = r.get("inventory", {}) or {}
        nr = Robot(id=r["id"], typ=r.get("type", "builder"),
                   pos=(float(pos[0]), float(pos[1])),
                   face=r.get("facing", "S"),
                   cap=int(inv.get("capacity", mod.cfg.carry_capacity)),
                   energy=float(r.get("energy", mod.cfg.energy_cap)),
                   state="idle",
                   ore=int(inv.get("ore", 0)), metal=int(inv.get("metal", 0)))
        wd.add_robot(nr)
        wd.pending_spawn.append(nr.id)
