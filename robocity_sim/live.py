"""Small stdlib-only helpers for talking to the live SimCode server.

Used by the ``robocity-sim`` CLI to (a) resolve *which* city this repo is and its
world seed — so a local run uses your city's actual map — and (b) back the
``inspect`` command (state / status / logs / exceptions). Everything is fetched
from the server's **public REST API** (no token, no MCP) — the same endpoints the
shareable live page uses — with plain ``urllib`` so the tool has **no runtime
dependencies**.

The actual simulation no longer lives in this repo: ``robocity-sim run`` drives the
**real** game engine (downloaded on demand by the vendored :mod:`simcode._local` /
:mod:`simcode._engine_dl`), not a local re-implementation.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional

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


def public_logs(server: str, slug: str, limit: int = 100) -> dict:
    """Recent activity/log lines from the PUBLIC endpoint — no token. Same ring the
    live page and get_recent_logs show: {slug, count, logs:[...]}."""
    url = f"{server.rstrip('/')}/api/city/{slug}/logs?limit={int(limit)}"
    return _http_get_json(url)


def public_exceptions(server: str, slug: str, release: Optional[str] = None) -> dict:
    """Unhandled exceptions the controller has thrown, from the PUBLIC endpoint —
    no token. Grouped by type + file:line, each with a sample traceback and the
    log lines leading up to it. Defaults to the current release; pass release='all'
    (or a commit SHA) to widen. {slug, release, count, groups:[...]}."""
    url = f"{server.rstrip('/')}/api/city/{slug}/exceptions"
    if release:
        url += "?release=" + urllib.parse.quote(release)
    return _http_get_json(url)


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
