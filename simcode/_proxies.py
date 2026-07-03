"""The module-level read-model handles the user script imports.

``robots``, ``buildings``, ``world``, ``store`` are stateless singletons that
forward to the :class:`~simcode._state.StateReader` of the *current* dispatch.
Accessing them outside a handler raises a clear error.
"""

from __future__ import annotations

from ._context import current


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


class _StoreProxy:
    """City-wide store; reads current state, records writes onto the intent."""

    def _s(self):
        return current().state.store

    def __getitem__(self, key):
        return self._s()[key]

    def __setitem__(self, key, value):
        self._s()[key] = value

    def __contains__(self, key):
        return key in self._s()

    def get(self, key, default=None):
        return self._s().get(key, default)

    def setdefault(self, key, default=None):
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
