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
    "base_level_up", "quest_updated",
    # Supply-chain (#5): building-addressed processor/decommission events.
    "resource_produced", "production_blocked", "building_destroyed",
    "decommission_started",
    # Living economy (#42): fleet expiry + building maintenance.
    "robot_expired",         # {robot_id} — cumulative flight distance exceeded lifespan
    "maintenance_needed",    # {building_id, condition} — wearing building's condition low
    "building_stopped",      # {building_id} — condition hit 0, production halted
    "repair_complete",       # {building_id, robot_id, condition} — repair ran dry / hit full
]

# --- Commands (script -> GAME). Robots fly + haul + charge + repair; world.build
# and world.destroy are world-scoped (not robot-bound). ---
COMMANDS = [
    "move_to", "pick_up", "drop", "charge", "send", "cancel",
    "build", "build_robot", "base_cancel", "destroy",
    "repair",  # (#42) Mechanic drains held metal into a worn building's condition
]

# --- Building / robot enums (mirror of schema.go) ---
BUILDING_TYPES = [
    "base", "mining", "storage", "flying_station",
    # Supply-chain (#5) processors + upgraded buildings.
    "smelter", "wire_mill", "glassworks", "kiln",
    "assembler", "electronics_lab", "alloy_furnace",
    "module_assembler", "frame_shop",
    "deep_mine", "warehouse", "charging_tower",
]
ROBOT_STATES = ["idle", "moving", "charging", "hauling", "repairing", "blocked"]

# --- Robot types (#42): classes chosen at build_robot() time, level-gated. ---
ROBOT_TYPES = ["builder", "hauler", "scout", "mechanic", "heavy_hauler", "ranger"]
