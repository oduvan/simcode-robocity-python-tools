"""Event subscription API: ``on``, ``subscribe``, ``unsubscribe``.

Registering a handler does two things:
1. records it locally (multiple handlers per event, in registration order), and
2. tells GAME the city wants that event, by sending a ``subscribe`` envelope on
   ``city.<id>.subscribe`` — so GAME only dispatches subscribed events.

Decorators run at import time, possibly before the runtime has a Redis
connection. The registry therefore *defers* the wire-side subscribe until a
runtime attaches (``attach_runtime``), then flushes all pending subscriptions.
"""

from __future__ import annotations

from typing import Callable, Optional


class _Subscription:
    __slots__ = ("handler", "once")

    def __init__(self, handler: Callable, once: bool):
        self.handler = handler
        self.once = once


class HandlerRegistry:
    def __init__(self) -> None:
        # event -> ordered list of _Subscription
        self._subs: dict[str, list[_Subscription]] = {}
        self._runtime = None  # set by attach_runtime; has .send_subscribe(event, once, action)

    # ----- runtime wiring -----
    def attach_runtime(self, runtime) -> None:
        self._runtime = runtime
        # Tell GAME about everything already registered at import time.
        for event, once in self.subscription_specs():
            runtime.send_subscribe(event, once=once, action="subscribe")

    def detach_runtime(self) -> None:
        self._runtime = None

    @property
    def events(self):
        return list(self._subs.keys())

    def subscription_specs(self) -> list[tuple[str, bool]]:
        """The current ``(event, once)`` set — the source of truth for what to
        (re)send to GAME. Used for the initial register and the resilient
        re-subscribe (GAME may not be listening when the container starts)."""
        specs = []
        for event, subs in self._subs.items():
            if subs:
                specs.append((event, all(s.once for s in subs)))
        return specs

    # ----- mutation -----
    def add(self, event: str, handler: Callable, once: bool = False) -> Callable:
        subs = self._subs.setdefault(event, [])
        for s in subs:
            if s.handler is handler:
                return handler  # idempotent: duplicate subscribe is a no-op
        first = not subs
        subs.append(_Subscription(handler, once))
        if self._runtime is not None and first:
            # First subscriber for this event -> register it with GAME.
            self._runtime.send_subscribe(event, once=once, action="subscribe")
        return handler

    def remove(self, event: str, handler: Callable) -> None:
        subs = self._subs.get(event)
        if not subs:
            return
        kept = [s for s in subs if s.handler is not handler]
        if kept:
            self._subs[event] = kept
        else:
            del self._subs[event]
            if self._runtime is not None:
                self._runtime.send_subscribe(event, once=False, action="unsubscribe")

    def handlers_for(self, event: str) -> list[_Subscription]:
        return list(self._subs.get(event, []))

    def fired(self, event: str, sub: _Subscription) -> None:
        """Called after a ``once`` handler runs — self-remove it."""
        if sub.once:
            self.remove(event, sub.handler)

    def clear(self) -> None:
        """Test helper: drop all handlers (does not message GAME)."""
        self._subs.clear()


# Module-global registry shared by `on` / subscribe / unsubscribe.
registry = HandlerRegistry()


class _On:
    """``@on.spawn`` / ``@on("spawn")`` — register an event handler."""

    def __call__(self, event: str, once: bool = False):
        def deco(handler: Callable) -> Callable:
            return registry.add(event, handler, once=once)

        return deco

    def __getattr__(self, event: str):
        if event.startswith("_"):
            raise AttributeError(event)

        def deco(handler: Callable) -> Callable:
            return registry.add(event, handler, once=False)

        return deco


on = _On()


def subscribe(event: str, handler: Callable, once: bool = False) -> Callable:
    return registry.add(event, handler, once=once)


def unsubscribe(event: str, handler: Optional[Callable] = None) -> None:
    if handler is None:
        for s in registry.handlers_for(event):
            registry.remove(event, s.handler)
    else:
        registry.remove(event, handler)
