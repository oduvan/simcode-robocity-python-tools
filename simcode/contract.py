"""Wire envelopes, the outbound Intent, and the per-event accumulator.

This sits just above `_wire` (the frozen channel/name mirror). It defines how
the SDK *encodes* the messages that cross Boundary 2 (GAME <-> CODE):

- inbound  : an ``event`` envelope -> :class:`Event`
- outbound : a ``subscribe`` envelope, and one or more ``intent`` envelopes
             built from an :class:`Accumulator` after handlers run.

Envelope shapes mirror ``docs/communication.md`` ("Message envelope"):

    {"city": .., "type": "event",  "event": "arrived", "robot": "r1",
     "payload": {..}}                                    # GAME -> CODE
    {"city": .., "type": "intent", "robot": "r1",
     "commands": [{"cmd": "move_to", "args": [4, 9]}],
     "store": {..}, "logs": [..]}                        # CODE -> GAME
    {"city": .., "type": "subscribe", "action": "subscribe",
     "event": "arrived", "once": true}                   # CODE -> GAME
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import _wire as wire


def encode(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=False)


def decode(raw: Any) -> Any:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)


# --------------------------------------------------------------------------- #
# Inbound: events
# --------------------------------------------------------------------------- #
class Event:
    """An event delivered by GAME. Carries ``robot_id`` + payload fields.

    Payload keys are exposed as attributes, so a handler can write
    ``e.cells`` / ``e.type`` / ``e.position`` directly (per robot-api.md).
    """

    __slots__ = ("event", "robot_id", "payload", "_env")

    def __init__(self, envelope: dict):
        self._env = envelope
        self.event = envelope.get("event")
        self.robot_id = envelope.get("robot")
        self.payload = envelope.get("payload") or {}

    # ``e.robot`` alias + payload field access.
    def __getattr__(self, name: str) -> Any:
        if name == "robot":
            return self.robot_id
        payload = object.__getattribute__(self, "payload")
        if name in payload:
            return payload[name]
        raise AttributeError(name)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.payload:
            return self.payload[key]
        return self._env.get(key, default)

    def __repr__(self) -> str:
        return f"Event(event={self.event!r}, robot_id={self.robot_id!r}, payload={self.payload!r})"


def build_subscribe(city: str, event: str, once: bool, action: str = "subscribe") -> dict:
    return {
        "city": city,
        "type": wire.TYPE_SUBSCRIBE,
        "action": action,          # "subscribe" | "unsubscribe"
        "event": event,
        "once": bool(once),
    }


# --------------------------------------------------------------------------- #
# Outbound: intents
# --------------------------------------------------------------------------- #
@dataclass
class Intent:
    """One outbound command intent, addressed to a single target id.

    ``robot`` is the target id — a robot id for robot actions, or a Flying
    Station building id for station commands (``build_robot``/``base_cancel``).
    """

    city: str
    robot: str
    commands: list = field(default_factory=list)
    logs: list = field(default_factory=list)
    store: dict | None = None
    memory: dict | None = None

    def to_envelope(self) -> dict:
        env: dict = {
            "city": self.city,
            "type": wire.TYPE_INTENT,
            "robot": self.robot,
            "commands": self.commands,
        }
        if self.logs:
            env["logs"] = self.logs
        if self.store:
            env["store"] = self.store
        if self.memory:
            env["memory"] = self.memory
        return env


def make_command(cmd: str, *args: Any) -> dict:
    """Build a single command dict ``{cmd, args}``.

    The engine consumes **positional args only** (no kwargs). Each robot handle
    method maps its keyword call to the engine's fixed positional arg order.
    """
    return {"cmd": cmd, "args": list(args)}


class Accumulator:
    """Collects commands / logs / store + memory writes during one event.

    Flushed by the runtime after all handlers for the event have run, into one
    Intent per target that accumulated anything. City-wide ``store`` writes ride
    on a single intent (the event's robot if present, else a standalone one).
    """

    def __init__(self) -> None:
        self.commands: dict[str, list] = {}
        self.logs: dict[str, list] = {}
        self.memory: dict[str, dict] = {}
        self.store_writes: dict = {}

    def add_command(self, target: str, command: dict) -> None:
        self.commands.setdefault(target, []).append(command)

    def add_log(self, target: str, msg: str) -> None:
        self.logs.setdefault(target, []).append(str(msg))

    def set_memory(self, target: str, mem: dict) -> None:
        self.memory[target] = mem

    def set_store(self, key: str, value: Any) -> None:
        self.store_writes[key] = value

    def is_empty(self) -> bool:
        return not (self.commands or self.logs or self.memory or self.store_writes)

    def build_intents(self, city: str, primary: str | None) -> list[Intent]:
        targets = set(self.commands) | set(self.logs) | set(self.memory)

        # Deterministic order: the event's own robot first, then the rest sorted.
        ordered: list[str] = []
        if primary is not None and primary in targets:
            ordered.append(primary)
        ordered.extend(t for t in sorted(targets) if t != primary)

        intents: list[Intent] = []
        store_emitted = False
        for t in ordered:
            it = Intent(
                city=city,
                robot=t,
                commands=self.commands.get(t, []),
                logs=self.logs.get(t, []),
                memory=self.memory.get(t),
            )
            if not store_emitted and self.store_writes:
                it.store = dict(self.store_writes)
                store_emitted = True
            intents.append(it)

        # Store changed but no robot target -> standalone store-only intent.
        if self.store_writes and not store_emitted:
            intents.append(
                Intent(city=city, robot=primary or "", commands=[], store=dict(self.store_writes))
            )
        return intents
