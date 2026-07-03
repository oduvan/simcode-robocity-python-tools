"""robocity_sim — a local, offline simulator for the SimCode "Robot City
Builder" game.

It re-implements the server engine (game/modules/robot_city) in Python and drives
the *unchanged* vendored ``simcode`` client SDK, so a user's ``main.py`` runs
byte-for-byte the same as it would on the server — but locally, with no network.
"""

from .config import Config, default_config, CANONICAL_SEED
from .module import Module
from .driver import Simulation, run_simulation, SimResult

__all__ = [
    "Config", "default_config", "CANONICAL_SEED",
    "Module", "Simulation", "run_simulation", "SimResult",
]
__version__ = "0.1.0"
