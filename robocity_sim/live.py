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
import os
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional

CANONICAL_SEED = 7  # the module's canonical map seed (matches the engine default)


class WorldUnavailable(Exception):
    """The world the run was asked for could not be obtained.

    Raised instead of quietly substituting a different one. Two identical runs
    once produced two different worlds because a failed lookup fell back to the
    canonical map, and the only sign was one word in the banner (#73 req 2 /
    forum post 22). Nothing in this module may swallow a lookup failure.
    """


def _http_get_json(url: str) -> dict:
    """GET a public (no-auth) JSON endpoint."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(url: str, doc: dict) -> dict:
    """POST a JSON document to a public (no-auth) endpoint and decode the reply."""
    body = json.dumps(doc).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def slug_for_repo(server: str, repo: str) -> Optional[str]:
    """Resolve a repo ("owner/name") to its city slug via the PUBLIC endpoint — no
    token. Returns None when no city is linked to that repo (a real answer); raises
    :class:`WorldUnavailable` when the question could not be asked at all."""
    url = server.rstrip("/") + "/api/city-by-repo/" + repo.strip("/")
    try:
        return _http_get_json(url).get("slug")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise WorldUnavailable(
            f"could not look up the city for {repo} on {server}: HTTP {e.code}") from e
    except Exception as e:
        raise WorldUnavailable(
            f"could not reach {server} to look up the city for {repo}: {e}") from e


def public_snapshot(server: str, slug: str) -> dict:
    """Fetch a city's current world snapshot from the PUBLIC endpoint — no token.
    Same document the shareable live page uses (world/robots/buildings/tiles/…)."""
    url = server.rstrip("/") + "/api/city/" + slug + "/snapshot"
    return _http_get_json(url)


def seed_for_city(server: str, slug: str) -> int:
    """The world seed of a city. Raises :class:`WorldUnavailable` if unobtainable."""
    seed, _ = world_of_city(server, slug)
    return seed


def world_of_city(server: str, slug: str):
    """``(seed, city_config)`` from a city's public snapshot.

    Both come from the same ``world`` doc in ONE fetch, on purpose: borrowing the
    seed without the config gave you the city's MAP but not its WORLD — a city
    created with ``starting_fleet: 5`` ran locally with the module default (#50).

    Raises :class:`WorldUnavailable` when the city's world cannot be read. It used
    to return ``(None, None)`` and let the caller quietly run the canonical map
    instead; that is the #73 req 2 bug, and there is no failure mode here for which
    running a DIFFERENT world is a better answer than stopping.
    """
    try:
        snap = public_snapshot(server, slug)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise WorldUnavailable(
                f"no city '{slug}' on {server} (its snapshot is 404)") from exc
        raise WorldUnavailable(
            f"could not read city '{slug}' from {server}: HTTP {exc.code}") from exc
    except Exception as exc:
        raise WorldUnavailable(
            f"could not reach {server} to read city '{slug}': {exc}") from exc
    world = snap.get("world") or {}
    seed = world.get("seed")
    if seed is None:
        raise WorldUnavailable(
            f"city '{slug}' snapshot from {server} carries no world seed "
            f"(the city may not have ticked yet)")
    cfg = world.get("config")
    return int(seed), (cfg if isinstance(cfg, dict) else None)


def saved_world_of_city(server: str, slug: str) -> dict:
    """The city's resumable SAVE from the PUBLIC endpoint — no token.

    ``GET /api/city/<slug>/save`` returns the durable world checkpoint plus the
    city-wide store, which are stored under separate keys and are BOTH needed: the
    world alone gives a city that looks right and behaves wrong.

    Returns the response document. Raises :class:`WorldUnavailable` when there is
    no save (a brand-new city that has not checkpointed yet), when the city is
    unknown, or when the server cannot be reached — never a fallback.
    """
    url = server.rstrip("/") + "/api/city/" + slug + "/save"
    try:
        doc = _http_get_json(url)
    except urllib.error.HTTPError as exc:
        # The not-saved answer is a JSON 404 with a machine-readable reason, so it
        # can never be mistaken for a valid empty world.
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            reason, msg = body.get("reason"), body.get("error")
            if reason == "no_save":
                raise WorldUnavailable(
                    f"city '{slug}' has no saved world yet — it has not run long enough "
                    f"to checkpoint one. Run without --from-live to start a fresh world."
                ) from exc
            if reason == "unknown_city":
                raise WorldUnavailable(f"no city '{slug}' on {server}") from exc
            detail = f": {msg}" if msg else ""
        except WorldUnavailable:
            raise
        except Exception:
            pass
        raise WorldUnavailable(
            f"could not read the saved world of city '{slug}' from {server}: "
            f"HTTP {exc.code}{detail}") from exc
    except Exception as exc:
        raise WorldUnavailable(
            f"could not reach {server} to read the saved world of city '{slug}': {exc}") from exc

    save = doc.get("save")
    if not isinstance(save, dict) or "world" not in save:
        raise WorldUnavailable(
            f"the saved world of city '{slug}' from {server} is not usable "
            f"(no world in the save envelope)")
    return doc


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


# --------------------------------------------------------------------------- #
# the acceptance rule for user code (#73 req 3 / forum post 18)
# --------------------------------------------------------------------------- #
#
# Whether a repo is acceptable is decided in ONE place — the server's
# loader.ValidateSources, the same function a real deploy runs. This tool does NOT
# keep its own copy of the allow-lists; a copied list is exactly how `__slots__`
# came to pass locally and be refused on deploy. It posts the sources and takes
# the server's verdict.

# Files the deploy path counts, per language. Everything under the project dir is
# sent except the noise a repo never deploys (.git, caches, virtualenvs).
_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
              "node_modules", ".idea", ".vscode"}
_MAX_UPLOAD = 1 << 20  # matches the server's repo size cap


class ValidationUnavailable(Exception):
    """The acceptance rule could not be consulted (server unreachable, old server).

    Distinct from a rejection: "your code is not acceptable" and "I could not find
    out" must never look the same, or we are back to a local run that promises
    something it did not check.
    """


def collect_sources(project_dir: str) -> dict:
    """Read the repo's files as ``{relative/path: text}`` for validation.

    Mirrors what the deploy sees: everything in the repo tree, minus VCS/cache
    directories. Binary files are sent as empty strings — they count toward the
    file/size limits but are never scanned.

    Approximation, deliberately on the safe side: the deploy validates a fresh
    CLONE, so it sees only COMMITTED files, while this sees the working tree. The
    skip list drops the untracked noise (`.venv`, `__pycache__`, `node_modules`)
    that a clone would not contain; anything else uncommitted is sent, so a file
    you have not committed can only make this stricter than the deploy, never
    laxer. The verdict on the code itself is the server's, unchanged.
    """
    out: dict = {}
    total = 0
    for root, dirs, names in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, project_dir).replace(os.sep, "/")
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            total += size
            if total > _MAX_UPLOAD:
                # Past the server's own cap; send what we have and let it decide.
                return out
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    out[rel] = fh.read()
            except (OSError, UnicodeDecodeError):
                out[rel] = ""
    return out


def validate_sources(server: str, language: str, files: dict) -> dict:
    """Ask the server whether this code would be accepted on deploy.

    Returns the verdict document ``{ok, error, file, line, message, ...}``.
    Raises :class:`ValidationUnavailable` if the question could not be asked.
    """
    url = server.rstrip("/") + "/api/code/validate"
    try:
        return _http_post_json(url, {"language": language, "files": files})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValidationUnavailable(
                f"{server} has no /api/code/validate endpoint (server too old to "
                f"tell us what a deploy would accept)") from e
        raise ValidationUnavailable(
            f"could not check your code against {server}: HTTP {e.code}") from e
    except Exception as e:
        raise ValidationUnavailable(
            f"could not reach {server} to check your code: {e}") from e


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
