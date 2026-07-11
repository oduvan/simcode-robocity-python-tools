"""robocity_sim — the ``robocity-sim`` local-test CLI for the SimCode "Robot City
Builder" game.

It no longer re-implements the engine. ``robocity-sim run`` downloads the **real**
game engine (the exact binary the server runs) via the vendored ``simcode`` SDK and
drives your *unchanged* ``main.py`` against it — so a local run is byte-for-byte the
server's game logic, with no re-implementation to drift.
"""

__version__ = "0.2.0"
