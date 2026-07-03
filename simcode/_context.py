"""The per-event dispatch context.

The module-level handles the user imports (``robots``, ``buildings``, ``world``,
``store``) are stateless proxies; the *real* state + the outbound accumulator
live here, set for the duration of one event dispatch. A ``ContextVar`` keeps it
correct even if a future runtime dispatches concurrently.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from ._state import StateReader
    from .contract import Accumulator, Event


@dataclass
class DispatchContext:
    city: str
    state: "StateReader"
    accumulator: "Accumulator"
    event: "Event"


_current: contextvars.ContextVar[Optional[DispatchContext]] = contextvars.ContextVar(
    "simcode_dispatch", default=None
)


def set_context(ctx: Optional[DispatchContext]):
    return _current.set(ctx)


def reset_context(token) -> None:
    _current.reset(token)


def current() -> DispatchContext:
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError(
            "simcode read model / commands are only available inside an event "
            "handler (no active dispatch). Did you call this at import time?"
        )
    return ctx


def current_or_none() -> Optional[DispatchContext]:
    return _current.get()
