"""Wire envelopes, the outbound Intent, and the per-event accumulator.

This sits just above `_wire` (the frozen channel/name mirror). It defines how
the client library *encodes* the messages that cross Boundary 2 (GAME <-> CODE):

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
        # Private/dunder lookups must still raise so Python's own attribute
        # protocols (copy, pickle, repr/IPython helpers, …) behave normally.
        if name.startswith("_"):
            raise AttributeError(name)
        # A missing *public* payload field reads as None instead of raising —
        # the same rule `_Attr` uses in the read model, and for the same reason
        # (forum #29). Payload fields are `omitempty` on the wire, so a field the
        # docs list is routinely absent: `blocked` carries no `reason` for some
        # blocks, `robot_destroyed` none for others. Raising here took out the
        # WHOLE handler, and a raised handler leaves the robot uncommanded — so
        # one absent key read as a frozen city rather than as a bug on one line.
        # `e.get(name, default)` still exists for callers who want a default.
        return None

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.payload:
            return self.payload[key]
        return self._env.get(key, default)

    def __repr__(self) -> str:
        return f"Event(event={self.event!r}, robot_id={self.robot_id!r}, payload={self.payload!r})"


def build_subscribe(city: str, event: str, once: bool, action: str = "subscribe") -> dict:
    """Build a subscribe/unsubscribe envelope.

    ``cancel`` is THE field GAME reads to remove a subscription
    (contract.Subscribe has no ``action`` field at all — see engine.go's
    ``if sub.Cancel``). Sending only ``action`` made unsubscribe a silent no-op:
    the engine saw cancel=false and treated it as a re-subscribe (#49).
    ``action`` is kept because the Go client also sends it and it reads well in
    logs, but ``cancel`` is what actually takes effect.
    """
    return {
        "city": city,
        "type": wire.TYPE_SUBSCRIBE,
        "action": action,                       # human-readable; NOT read by GAME
        "cancel": action == "unsubscribe",      # the field GAME acts on
        "event": event,
        "once": bool(once),
    }


# --------------------------------------------------------------------------- #
# Outbound: intents
# --------------------------------------------------------------------------- #
@dataclass
class Intent:
    """One outbound command intent, addressed to a single target id.

    ``robot`` is the target id — a robot id for robot actions, or the Base
    building id for direct Base commands (``build_robot``/``base_cancel``).
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


def _fingerprint(value: Any) -> str:
    """A stable string for a store value, for spotting in-place mutation.

    `default=repr` so an exotic value can never make taking the fingerprint
    raise — a value that cannot be encoded will fail later, where it already
    did, rather than here where nothing failed before.
    """
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
    except Exception:  # pragma: no cover - belt and braces; see the docstring
        return repr(value)


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
        # The live city store, plus how each top-level value looked when this
        # event started. See `watch_store` / `_store_payload` (forum #34).
        self._store_live: dict | None = None
        self._store_before: dict[str, str] = {}

    def add_command(self, target: str, command: dict) -> None:
        self.commands.setdefault(target, []).append(command)

    def add_log(self, target: str, msg: str) -> None:
        self.logs.setdefault(target, []).append(str(msg))

    def set_memory(self, target: str, mem: dict) -> None:
        self.memory[target] = mem

    def set_store(self, key: str, value: Any) -> None:
        self.store_writes[key] = value

    # ----------------------------------------------------------------- #
    # nested store writes (forum #34)
    #
    # GAME merges the store by TOP-LEVEL key, so only `store[k] = v` was ever
    # recorded. But `store["jobs"][rid] = {...}` is the obvious way to write it,
    # and it mutated the backing dict in place: it read back correctly for the
    # rest of the handler and looked right in testing, then vanished on reload.
    # Silent data loss is the worst failure this library had.
    #
    # So the whole store is fingerprinted when the event starts and compared when
    # the intents are built. Any top-level key whose value CHANGED — however deep
    # the mutation, through dicts, lists, anything — is sent whole. This is exact
    # in both directions: a key that was only read is not sent (mergeStore marks
    # every written key changed, so over-sending would put it in every delta),
    # and no depth of mutation escapes it. The store is small (~2 KB) and this is
    # one dump per event, so the cost does not signify.
    # ----------------------------------------------------------------- #
    def watch_store(self, live: dict) -> None:
        """Fingerprint the store as the event found it."""
        self._store_live = live
        self._store_before = {k: _fingerprint(v) for k, v in live.items()}

    def _store_payload(self) -> dict:
        """Explicit writes, plus any key mutated in place under our feet."""
        out = dict(self.store_writes)
        if self._store_live is None:
            return out
        for k, v in self._store_live.items():
            if k in out:
                continue  # an explicit write already carries the new value
            if self._store_before.get(k) != _fingerprint(v):
                out[k] = v
        return out

    def is_empty(self) -> bool:
        return not (self.commands or self.logs or self.memory or self._store_payload())


    def build_intents(self, city: str, primary: str | None) -> list[Intent]:
        targets = set(self.commands) | set(self.logs) | set(self.memory)

        # Deterministic order: the event's own robot first, then the rest sorted.
        ordered: list[str] = []
        if primary is not None and primary in targets:
            ordered.append(primary)
        ordered.extend(t for t in sorted(targets) if t != primary)

        store_payload = self._store_payload()

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
            if not store_emitted and store_payload:
                it.store = dict(store_payload)
                store_emitted = True
            intents.append(it)

        # Store changed but no robot target -> standalone store-only intent.
        if store_payload and not store_emitted:
            intents.append(
                Intent(city=city, robot=primary or "", commands=[], store=dict(store_payload))
            )
        return intents
