"""Download + cache a game module's compiled engine shared library from the server.

Local testing (:mod:`simcode._local`) drives the **real** engine over FFI. The
engine is PER GAME MODULE (Robot City has its own, a future module ships its own),
so everything here is keyed by the ``module`` type. Rather than ask users to build
``libengine.so`` themselves, we fetch the EXACT engine the server runs for that
module from its distribution endpoint (#29):

* ``GET  {server}/api/engine/version?module=<m>``            → ``{"module","version","platforms"}``
* ``GET  {server}/api/engine/lib?module=<m>&os=..&arch=..``  → the raw ``.so`` bytes

The library is cached at ``~/.cache/simcode/engine-<module>-<version>-<platform>.so``
and re-used on later runs (skip re-download when the cached module+version matches).
The server base URL is ``$SIMCODE_SERVER`` (default ``https://robocity.lyabah.com``).

The engine is **glibc**-linked, so it can only be dlopen'd by a glibc Python
(``python:*-slim``); musl/alpine cannot load it. This module doesn't enforce that
(the ctypes load in ``_local`` surfaces the real error), but see the note in
:func:`local_platform`.
"""

from __future__ import annotations

import json
import os
import platform
import urllib.error
import urllib.request

DEFAULT_SERVER = "https://robocity.lyabah.com"


class EngineDownloadError(RuntimeError):
    """Raised when the engine library can't be resolved (server unreachable, the
    platform isn't served, a bad response, …) — always with an actionable message."""


def server_base() -> str:
    """The server base URL: ``$SIMCODE_SERVER`` or the public default (no slash)."""
    return os.environ.get("SIMCODE_SERVER", DEFAULT_SERVER).rstrip("/")


def local_platform() -> str:
    """Detect this machine's platform token (e.g. ``linux-amd64``) used by the
    server's filenames / query params.

    Maps ``platform.system()``/``machine()`` to ``<os>-<arch>``:
    ``Linux``→``linux``, ``Darwin``→``darwin``, ``Windows``→``windows``;
    ``x86_64``/``amd64``→``amd64``, ``aarch64``/``arm64``→``arm64``.
    """
    sysname = platform.system().lower()
    os_map = {"linux": "linux", "darwin": "darwin", "windows": "windows"}
    osname = os_map.get(sysname, sysname)
    mach = platform.machine().lower()
    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    arch = arch_map.get(mach, mach)
    return f"{osname}-{arch}"


def cache_dir() -> str:
    """The per-user cache dir (``$XDG_CACHE_HOME`` aware), created on demand."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    d = os.path.join(base, "simcode")
    os.makedirs(d, exist_ok=True)
    return d


def _http_get(url: str, timeout: float = 30.0):
    try:
        return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 (trusted URL)
    except urllib.error.HTTPError as e:
        raise EngineDownloadError(f"GET {url} → HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise EngineDownloadError(
            f"GET {url} failed ({e.reason}); is the server reachable? "
            f"Set SIMCODE_SERVER or SIMCODE_ENGINE_SO to override."
        ) from e


def fetch_version(module: str, server: str | None = None) -> tuple[str, list[str]]:
    """Return ``(version, platforms)`` from ``/api/engine/version?module=<module>``.

    Raises :class:`EngineDownloadError` if the server is unreachable, the module has
    no engine (404), or the engine distribution isn't available (503)."""
    server = (server or server_base()).rstrip("/")
    url = f"{server}/api/engine/version?module={module}"
    with _http_get(url) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    version = doc.get("version")
    platforms = doc.get("platforms") or []
    if not version:
        raise EngineDownloadError(f"{url} returned no version for module {module!r}")
    return version, platforms


def ensure_engine(
    module: str, server: str | None = None, platform_token: str | None = None
) -> str:
    """Resolve a usable engine ``.so`` path for ``module`` on this machine,
    downloading + caching it from the server if needed. Returns the local path.

    * ``$SIMCODE_ENGINE_SO`` (if set) wins — an explicit dev override (any module).
    * else GET the module's server version, and if
      ``engine-<module>-<version>-<platform>.so`` is already cached, return it;
      otherwise download and cache it.
    """
    override = os.environ.get("SIMCODE_ENGINE_SO")
    if override:
        if not os.path.exists(override):
            raise EngineDownloadError(
                f"SIMCODE_ENGINE_SO={override!r} does not exist"
            )
        return override

    server = (server or server_base()).rstrip("/")
    plat = platform_token or local_platform()
    version, platforms = fetch_version(module, server)

    if platforms and plat not in platforms:
        raise EngineDownloadError(
            f"the server has no {module!r} engine library for this platform ({plat}); "
            f"available: {', '.join(platforms)}. "
            f"Build one locally and point SIMCODE_ENGINE_SO at it, or run local "
            f"tests on a linux-amd64 (glibc) host."
        )

    cached = os.path.join(cache_dir(), f"engine-{module}-{version}-{plat}.so")
    if os.path.exists(cached) and os.path.getsize(cached) > 0:
        return cached

    os_, _, arch = plat.partition("-")
    url = f"{server}/api/engine/lib?module={module}&os={os_}&arch={arch}"
    with _http_get(url) as resp:
        if resp.status == 404:  # pragma: no cover (HTTPError raised first)
            raise EngineDownloadError(
                f"no {module!r} engine library for platform {plat}"
            )
        data = resp.read()
    if not data:
        raise EngineDownloadError(f"{url} returned an empty library")

    # Write atomically (temp + rename) so a concurrent run never sees a half file.
    tmp = f"{cached}.{os.getpid()}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, cached)
    return cached
