"""A tiny in-process fake Redis.

The vendored SDK runtime talks to Redis for exactly three things during a local
run:

* ``mget(keys)`` / ``get(key)`` — read the ``city.<id>.state.*`` KV (the engine
  writes these each tick as JSON strings),
* ``publish(channel, msg)`` — subscription registration (we don't need GAME, so
  it's captured and ignored),
* ``xadd(stream, fields)`` — intent emission (we read intents from
  ``Runtime.dispatch``'s return value instead, so this is captured only).

Nothing else in :class:`simcode.Runtime.dispatch` touches the client.
"""

from __future__ import annotations

from typing import Dict, List, Optional


class FakeRedis:
    def __init__(self) -> None:
        self._kv: Dict[str, str] = {}
        self.published: List[tuple] = []
        self.xadded: List[tuple] = []

    # --- state store (engine writes, SDK reads) ---------------------------- #
    def set_state(self, key: str, value: str) -> None:
        self._kv[key] = value

    def mset_state(self, mapping: Dict[str, str]) -> None:
        self._kv.update(mapping)

    def get(self, key: str) -> Optional[str]:
        return self._kv.get(key)

    def mget(self, keys) -> List[Optional[str]]:
        return [self._kv.get(k) for k in keys]

    # --- pub/sub + streams (captured, not needed to drive) ----------------- #
    def publish(self, channel: str, message) -> int:
        self.published.append((channel, message))
        return 0

    def xadd(self, stream: str, fields, *args, **kwargs) -> str:
        self.xadded.append((stream, fields))
        return f"0-{len(self.xadded)}"
