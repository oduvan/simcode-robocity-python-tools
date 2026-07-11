"""Small stdlib-only helpers for talking to the live SimCode server.

Used by the ``robocity-sim`` CLI to (a) resolve *which* city this repo is and its
world seed — so a local run uses your city's actual map — and (b) back the
``inspect`` command (state/status/logs). Everything here is plain ``urllib`` so the
tool has **no runtime dependencies**.

The actual simulation no longer lives in this repo: ``robocity-sim run`` drives the
**real** game engine (downloaded on demand by the vendored :mod:`simcode._local` /
:mod:`simcode._engine_dl`), not a local re-implementation.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
import urllib.error
from typing import Optional

DEFAULT_SERVER = "https://robocity.lyabah.com"
CANONICAL_SEED = 7  # the module's canonical map seed (matches the engine default)


def _http_get_json(url: str) -> dict:
    """GET a public (no-auth) JSON endpoint."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def slug_for_repo(server: str, repo: str) -> Optional[str]:
    """Resolve a repo ("owner/name") to its city slug via the PUBLIC endpoint —
    no token. Returns None if no city is linked to that repo."""
    url = server.rstrip("/") + "/api/city-by-repo/" + repo.strip("/")
    try:
        return _http_get_json(url).get("slug")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def public_snapshot(server: str, slug: str) -> dict:
    """Fetch a city's current world snapshot from the PUBLIC endpoint — no token.
    Same document the shareable live page uses (world/robots/buildings/tiles/…)."""
    url = server.rstrip("/") + "/api/city/" + slug + "/snapshot"
    return _http_get_json(url)


def seed_for_city(server: str, slug: str) -> Optional[int]:
    """The world seed of a city, from its public snapshot — so a local run uses the
    same map as your live city. Returns None if it can't be fetched."""
    try:
        snap = public_snapshot(server, slug)
    except Exception:
        return None
    seed = (snap.get("world") or {}).get("seed")
    return int(seed) if seed is not None else None


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
