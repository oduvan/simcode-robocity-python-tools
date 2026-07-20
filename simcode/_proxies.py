"""The module-level read-model handles the user script imports.

``robots``, ``buildings``, ``world``, ``store`` are stateless singletons that
forward to the :class:`~simcode._state.StateReader` of the *current* dispatch.
Accessing them outside a handler raises a clear error.
"""

from __future__ import annotations

import json

from ._context import current

# The city-wide store + robot memory are persisted as JSON (Redis in prod, the
# tick request locally), so their values must be JSON-serializable. Catch the
# common mistake (a set, a custom object) at ASSIGNMENT time with a message that
# names the key, instead of a cryptic failure later when the store is flushed.
_JSON_SCALARS = (str, int, float, bool, type(None))


def _ensure_json(key, value):
    if isinstance(value, _JSON_SCALARS):
        return
    try:
        json.dumps(value)
    except TypeError:
        raise TypeError(
            f"store[{key!r}] = {type(value).__name__} is not JSON-serializable; the "
            f"store only holds JSON types (str/int/float/bool/list/dict/None) — e.g. "
            f"use a list instead of a set."
        ) from None


class _RobotsProxy:
    def __getitem__(self, robot_id):
        return current().state.robots[robot_id]

    def __contains__(self, robot_id):
        return robot_id in current().state.robots

    def all(self):
        return current().state.robots.all()

    def of_type(self, t):
        return current().state.robots.of_type(t)

    def __iter__(self):
        return iter(current().state.robots)

    def __len__(self):
        return len(current().state.robots)


class _BuildingsProxy:
    def __getitem__(self, building_id):
        return current().state.buildings[building_id]

    def __contains__(self, building_id):
        return building_id in current().state.buildings

    def all(self):
        return current().state.buildings.all()

    def of_type(self, t):
        return current().state.buildings.of_type(t)

    def stations(self):
        """All Flying Station handles (each carries ``build_robot`` / ``cancel``)."""
        return current().state.buildings.stations()

    @property
    def base(self):
        return current().state.buildings.base

    def __iter__(self):
        return iter(current().state.buildings)


class _WorldProxy:
    @property
    def tick(self):
        return current().state.world.tick

    @property
    def size(self):
        return current().state.world.size

    @property
    def origin(self):
        return current().state.world.origin

    @property
    def endless(self):
        return current().state.world.endless

    @property
    def seed(self):
        return current().state.world.seed

    @property
    def discovered(self):
        return current().state.world.discovered

    def spots(self):
        return current().state.world.spots()

    def cells(self, region=None):
        return current().state.world.cells(region)

    def build(self, type, x, y):
        """Place a construction site of ``type`` at (x, y) — world-scoped."""
        return current().state.world.build(type, x, y)

    def destroy(self, x, y):
        """Decommission the building at (x, y) — world-scoped (#5)."""
        return current().state.world.destroy(x, y)


class _StoreProxy:
    """City-wide store; reads current state, records writes onto the intent."""

    def _s(self):
        return current().state.store

    def __getitem__(self, key):
        return self._s()[key]

    def __setitem__(self, key, value):
        _ensure_json(key, value)
        self._s()[key] = value

    def __contains__(self, key):
        return key in self._s()

    def get(self, key, default=None):
        return self._s().get(key, default)

    def setdefault(self, key, default=None):
        if key not in self._s():
            _ensure_json(key, default)
        return self._s().setdefault(key, default)

    def keys(self):
        return self._s().keys()

    def items(self):
        return self._s().items()

    def values(self):
        return self._s().values()

    def __iter__(self):
        return iter(self._s())

    def __len__(self):
        return len(self._s())


robots = _RobotsProxy()
buildings = _BuildingsProxy()
world = _WorldProxy()
store = _StoreProxy()
