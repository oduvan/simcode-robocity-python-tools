"""Wire protocol mirror — keep in lockstep with game/core/contract (Go).

This module is the Python source of truth for channel names, event/command
names, and the envelope shapes. The user-facing API (`on`, `robots`,
`buildings`) is built on top of these in Phase 1.
"""

# --- Envelope type tags ---
TYPE_DELTA = "delta"
TYPE_EVENT = "event"
TYPE_INTENT = "intent"
TYPE_CONTROL = "control"
TYPE_SUBSCRIBE = "subscribe"
TYPE_SNAPSHOT = "snapshot"


def channels(city_id: str) -> dict:
    """Fully-qualified Redis channels/keys for a city (mirror of ChannelsFor)."""
    p = f"city.{city_id}."
    return {
        "state_prefix": p + "state.",
        "delta": p + "delta",
        "snapshot": p + "snapshot",
        "control": p + "control",
        "lifecycle": p + "lifecycle",
        "code": p + "code",
        "subscribe": p + "subscribe",
        "event": p + "event",
        "intent": p + "intent",
        "log": p + "log",
        "metrics": p + "metrics",
        "introspect": p + "introspect",
    }


def state_key(city_id: str, name: str) -> str:
    return f"city.{city_id}.state.{name}"


def store_key(city_id: str) -> str:
    """Durable, game-agnostic user-store key (mirror of contract.StoreKey). GAME
    write-throughs the city-wide ``store`` here; the SDK reads it on (re)connect to
    restore the store so a reloaded controller sees its prior values."""
    return f"city.{city_id}.store"


def acl_key_pattern(city_id: str) -> str:
    return f"city.{city_id}.*"


# --- Events (GAME -> script). Frozen for v1; mirror of contract/names.go. ---
EVENTS = [
    "spawn", "tick", "idle", "arrived", "blocked",
    "construction_started", "resource_delivered", "construction_complete",
    "spot_depleted", "storage_full", "inventory_full",
    "robot_produced", "robot_destroyed", "charge_complete", "message",
]

# --- Commands (script -> GAME). Robots fly + haul + charge; world.build is
# world-scoped (not robot-bound). ---
COMMANDS = [
    "move_to", "pick_up", "drop", "charge", "send", "cancel",
    "build", "build_robot", "base_cancel",
]

# --- Building / robot enums (mirror of schema.go) ---
BUILDING_TYPES = ["base", "mining", "storage", "flying_station"]
ROBOT_STATES = ["idle", "moving", "charging", "hauling", "blocked"]
