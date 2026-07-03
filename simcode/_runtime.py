"""The CODE-container runtime: Redis connection + the event->intent loop.

Responsibilities:
- own the city-scoped Redis connection (addr from ``REDIS_ADDR``, city from
  ``SIMCODE_CITY``),
- register the city's subscriptions with GAME (``city.<id>.subscribe``),
- consume events on ``city.<id>.event``, build a fresh read model from
  ``city.<id>.state.*``, dispatch to matching handlers in registration order,
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
        self.pubsub = None
        self._running = False
        # In-process persistence (engine does not yet round-trip these via
        # state.* — TODO persistence phase). Live for the process; reset on
        # hot-reload when the script module is re-imported.
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

    def open(self) -> "Runtime":
        """Subscribe to the inbound event channel."""
        self.pubsub = self.redis.pubsub()
        self.pubsub.subscribe(self.ch["event"])
        return self

    def close(self) -> None:
        self._running = False
        if self.pubsub is not None:
            try:
                self.pubsub.unsubscribe(self.ch["event"])
            except Exception:
                pass
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
    def pump(self, max_messages: int | None = None) -> int:
        """Drain currently-available events. Returns how many were dispatched."""
        if self.pubsub is None:
            self.open()
        n = 0
        while max_messages is None or n < max_messages:
            msg = self.pubsub.get_message(ignore_subscribe_messages=True, timeout=0.0)
            if not msg:
                break
            if msg.get("type") != "message":
                continue
            try:
                envelope = decode(msg["data"])
            except Exception:
                continue
            self.dispatch(envelope)
            n += 1
        return n

    def run_forever(self, poll_timeout: float = 1.0) -> None:
        if self.pubsub is None:
            self.open()
        self._running = True
        while self._running:
            # Keep re-sending subscriptions until GAME answers (see __init__).
            # get_message returns after `poll_timeout` even with no traffic, so
            # this runs periodically without a separate thread.
            self.maybe_resubscribe()
            msg = self.pubsub.get_message(ignore_subscribe_messages=True, timeout=poll_timeout)
            if not msg or msg.get("type") != "message":
                continue
            try:
                envelope = decode(msg["data"])
            except Exception:
                continue
            self.dispatch(envelope)


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

    rt = Runtime(redis_client, city).install().open()
    rt.run_forever(poll_timeout=poll_timeout)
    return rt
