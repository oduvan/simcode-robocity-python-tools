"""The CODE-container runtime: Redis connection + the event->intent loop.

Responsibilities:
- own the city-scoped Redis connection (addr from ``REDIS_ADDR``, city from
  ``SIMCODE_CITY``),
- register the city's subscriptions with GAME (``city.<id>.subscribe``),
- consume events off the DURABLE stream ``city.<id>.event`` via a consumer group
  (``code`` / ``code-<city>``) so events survive a container restart (no loss,
  order preserved), build a fresh read model from ``city.<id>.state.*``, dispatch
  to matching handlers in registration order,
- flush the accumulated commands/store/logs as intents on the durable stream
  ``city.<id>.intent``.

The untrusted user script never reaches Redis — only this runtime does. The
sandbox that isolates the script is a separate concern (TODO: sandbox-security).
"""

from __future__ import annotations

import os
import time
import traceback

from . import _wire as wire
from ._context import DispatchContext, reset_context, set_context
from ._registry import registry
from ._state import StateReader
from .contract import Accumulator, Event, build_subscribe, decode, encode

# State sub-keys — each a plain JSON string the engine writes (see _state.py).
_STATE_KEYS = ("meta", "world", "robots", "buildings", "tiles", "discovered")

# Durable event stream: consumer group + this container's consumer name. The
# group persists across restarts, so delivered-but-unacked entries from a prior
# crash are reclaimed (read id "0") before new entries (read id ">").
_EVENT_GROUP = "code"
_EVENT_FIELD = "data"


def _redis_from_addr(addr: str):
    import redis  # imported lazily so tests can run with a fake client

    if "://" in addr:
        return redis.Redis.from_url(addr, decode_responses=True)
    host, _, port = addr.partition(":")
    return redis.Redis(host=host or "localhost", port=int(port or 6379), decode_responses=True)


class Runtime:
    def __init__(self, redis_client, city: str):
        self.redis = redis_client
        self.city = city
        self.ch = wire.channels(city)
        self._consumer = f"{_EVENT_GROUP}-{city}"
        self._group_ready = False
        # Read the consumer's own PENDING (delivered-but-unacked) backlog first
        # by starting at id "0"; flip to ">" (new entries only) once drained.
        self._read_id = "0"
        self._running = False
        # City-wide ``store`` is durable: GAME persists it and the SDK restores it
        # on connect (see restore_store), so it survives a hot-reload / container
        # restart. Per-robot ``memory`` is still in-process (reset on hot-reload).
        self.store_state: dict = {}
        self.memory_state: dict = {}
        # Resilient subscription delivery. A CODE container can start before its
        # GAME world is up; the initial `subscribe` messages go over lossy
        # pub/sub and are dropped if GAME isn't listening yet, leaving the
        # container silent forever. So re-send the full subscription set every
        # `resubscribe_interval` until the first event arrives (or we give up
        # after `resubscribe_cap`). Idempotent on the GAME side.
        self.resubscribe_interval = 2.0
        self.resubscribe_cap = 30.0
        self._first_event = False
        self._resub_started: float | None = None
        self._last_resub: float | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def install(self) -> "Runtime":
        """Attach to the global registry and register existing subscriptions."""
        registry.attach_runtime(self)
        return self

    def restore_store(self) -> "Runtime":
        """Load the durable city-wide ``store`` from GAME into ``store_state`` so a
        reloaded/restarted controller sees its prior values instead of an empty
        dict. The store is a plain JSON object at ``city.<id>.store`` (key -> value)
        that GAME write-throughs on each store merge. Missing/empty/unreadable is a
        no-op (fresh empty store). The write path is unchanged — writes still ride
        out on the intent's ``store`` field."""
        try:
            raw = self.redis.get(wire.store_key(self.city))
        except Exception:
            return self
        if not raw:
            return self
        try:
            data = decode(raw)
        except Exception:
            return self
        if isinstance(data, dict):
            self.store_state.clear()
            self.store_state.update(data)
        return self

    def open(self) -> "Runtime":
        """Ensure the durable event stream's consumer group exists.

        Uses ``MKSTREAM`` so the stream is created if GAME hasn't produced yet,
        and starts the group at ``$`` (only entries appended after creation). The
        group persists across restarts — that persistence is what makes events
        resumable — so a re-created group with the same name is a BUSYGROUP we
        deliberately ignore. Must run BEFORE the initial subscriptions are sent,
        so GAME's replay-on-subscribe events land after the group exists.
        """
        if not self._group_ready:
            try:
                self.redis.xgroup_create(
                    self.ch["event"], _EVENT_GROUP, id="$", mkstream=True
                )
            except Exception as exc:  # BUSYGROUP: group already exists — fine.
                if "BUSYGROUP" not in str(exc):
                    raise
            self._group_ready = True
        return self

    def close(self) -> None:
        self._running = False
        registry.detach_runtime()

    # ------------------------------------------------------------------ #
    # subscriptions (CODE -> GAME)
    # ------------------------------------------------------------------ #
    def send_subscribe(self, event: str, once: bool = False, action: str = "subscribe") -> None:
        msg = build_subscribe(self.city, event, once, action=action)
        self.redis.publish(self.ch["subscribe"], encode(msg))

    def _resend_subscriptions(self) -> None:
        """Re-publish the full current subscription set (the source of truth is
        the registry, so any runtime-added subscription is covered too)."""
        for event, once in registry.subscription_specs():
            self.send_subscribe(event, once=once, action="subscribe")

    def maybe_resubscribe(self, now: float | None = None) -> bool:
        """Re-send subscriptions if GAME may have missed them. No-op once the
        first event has been received or the cap window has elapsed. Returns
        True iff it re-published this call. Driven from the poll loop (no extra
        thread); ``now`` is injectable for tests."""
        if self._first_event:
            return False
        if now is None:
            now = time.monotonic()
        if self._resub_started is None:
            # Baseline: the initial subscriptions were just sent on install().
            self._resub_started = now
            self._last_resub = now
            return False
        if now - self._resub_started > self.resubscribe_cap:
            return False  # gave up — assume something else is wrong
        if now - self._last_resub >= self.resubscribe_interval:
            self._resend_subscriptions()
            self._last_resub = now
            return True
        return False

    # ------------------------------------------------------------------ #
    # state read model (GAME state.* -> read model)
    # ------------------------------------------------------------------ #
    def _read_state(self, accumulator: Accumulator) -> StateReader:
        keys = [wire.state_key(self.city, n) for n in _STATE_KEYS]
        try:
            vals = self.redis.mget(keys)
        except AttributeError:  # client without mget -> fall back to GET
            vals = [self.redis.get(k) for k in keys]
        raw = dict(zip(_STATE_KEYS, vals))

        def parse(name: str, default):
            v = raw.get(name)
            return decode(v) if v else default

        return StateReader(
            meta=parse("meta", {}),
            world=parse("world", {}),
            robots=parse("robots", []),
            buildings=parse("buildings", []),
            tiles=parse("tiles", []),
            discovered=raw.get("discovered"),
            store_state=self.store_state,
            memory_state=self.memory_state,
            accumulator=accumulator,
        )

    # ------------------------------------------------------------------ #
    # dispatch (the core of data-in / intents-out)
    # ------------------------------------------------------------------ #
    def dispatch(self, envelope: dict) -> list:
        """Dispatch one event envelope; return the list of intent envelopes."""
        # First contact with GAME — stop the resilient re-subscribe loop.
        self._first_event = True
        event = Event(envelope)
        subs = registry.handlers_for(event.event)
        if not subs:
            return []

        accumulator = Accumulator()
        state = self._read_state(accumulator)
        ctx = DispatchContext(city=self.city, state=state, accumulator=accumulator, event=event)
        token = set_context(ctx)
        try:
            for sub in subs:
                try:
                    sub.handler(event)
                except Exception:  # one bad handler must not kill the loop
                    self._report_error(event, sub.handler)
                finally:
                    registry.fired(event.event, sub)
        finally:
            reset_context(token)

        intents = accumulator.build_intents(self.city, event.robot_id)
        for it in intents:
            self._publish_intent(it.to_envelope())
        return [it.to_envelope() for it in intents]

    def _publish_intent(self, envelope: dict) -> None:
        # Intents are must-not-drop -> durable stream (xadd), not pub/sub.
        self.redis.xadd(self.ch["intent"], {"data": encode(envelope)})

    def _report_error(self, event: Event, handler) -> None:
        tb = traceback.format_exc()
        name = getattr(handler, "__name__", repr(handler))
        try:
            self.redis.publish(
                self.ch["log"],
                encode(
                    {
                        "city": self.city,
                        "type": "log",
                        "level": "error",
                        "event": event.event,
                        "robot": event.robot_id,
                        "handler": name,
                        "error": tb,
                    }
                ),
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # loop
    # ------------------------------------------------------------------ #
    def _consume_batch(self, count: int | None, block: int | None) -> tuple[int, int]:
        """Read one XREADGROUP batch, dispatch + XACK each entry.

        Returns ``(dispatched, consumed)`` where ``consumed`` counts every entry
        seen (including undecodable ones, which are still ACKed so a bad entry is
        not redelivered forever — the intent consumer does the same) and
        ``dispatched`` counts events actually handed to :meth:`dispatch`. Reads
        this consumer's PENDING backlog (id ``"0"``) first; once that drains it
        flips to new entries (id ``">"``)."""
        if not self._group_ready:
            self.open()
        try:
            res = self.redis.xreadgroup(
                _EVENT_GROUP,
                self._consumer,
                {self.ch["event"]: self._read_id},
                count=count,
                block=block,
            )
        except Exception:
            return 0, 0
        dispatched = 0
        consumed = 0
        for _stream, entries in (res or []):
            for entry_id, fields in entries:
                consumed += 1
                raw = fields.get(_EVENT_FIELD) if fields else None
                if raw is not None:
                    try:
                        self.dispatch(decode(raw))
                        dispatched += 1
                    except Exception:
                        pass  # bad entry: ACK below, don't redeliver
                self.redis.xack(self.ch["event"], _EVENT_GROUP, entry_id)
        # Pending backlog exhausted -> switch to new entries only.
        if self._read_id == "0" and consumed == 0:
            self._read_id = ">"
        return dispatched, consumed

    def pump(self, max_messages: int | None = None) -> int:
        """Drain currently-available events. Returns how many were dispatched."""
        if not self._group_ready:
            self.open()
        total = 0
        while max_messages is None or total < max_messages:
            count = None if max_messages is None else max(1, max_messages - total)
            was_pending = self._read_id == "0"
            dispatched, consumed = self._consume_batch(count=count, block=None)
            total += dispatched
            if consumed == 0:
                if was_pending and self._read_id == ">":
                    continue  # just drained the pending backlog; read new entries
                break  # nothing left to drain
        return total

    def run_forever(self, poll_timeout: float = 1.0) -> None:
        if not self._group_ready:
            self.open()
        self._running = True
        block_ms = max(1, int(poll_timeout * 1000))
        while self._running:
            # Keep re-sending subscriptions until GAME answers (see __init__).
            self.maybe_resubscribe()
            if self._read_id == "0":
                # Drain the delivered-but-unacked backlog non-blocking, then loop
                # (which flips _read_id to ">" once the backlog is empty).
                self._consume_batch(count=256, block=None)
            else:
                # Block up to poll_timeout for new entries, so maybe_resubscribe
                # still runs periodically without a separate thread.
                self._consume_batch(count=256, block=block_ms)


def run(redis_client=None, city: str | None = None, poll_timeout: float = 1.0) -> Runtime:
    """Entrypoint the CODE container calls after importing the user script.

    With no args, reads ``REDIS_ADDR`` and ``SIMCODE_CITY`` from the env and
    blocks in the event loop. Tests pass a fake client + city and drive
    :meth:`Runtime.pump` instead.
    """
    if city is None:
        city = os.environ.get("SIMCODE_CITY")
    if not city:
        raise RuntimeError("SIMCODE_CITY not set (no city id for this CODE container)")
    if redis_client is None:
        addr = os.environ.get("REDIS_ADDR", "localhost:6379")
        redis_client = _redis_from_addr(addr)

    # Create the event stream's consumer group (open) BEFORE registering
    # subscriptions (install), so GAME's replay-on-subscribe events land in the
    # stream after the group exists and are therefore delivered, not lost.
    # restore_store repopulates the durable city-wide store before the first event.
    rt = Runtime(redis_client, city).open().restore_store().install()
    rt.run_forever(poll_timeout=poll_timeout)
    return rt
