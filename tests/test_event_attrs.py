"""forum #29: a payload field the event does not carry must read as None.

`_Attr` already made that promise for the read model, and said why in its own
comment: a defensive `attr.field or default` over an omitempty wire field must
never crash a handler. `Event` took the opposite line for the same class of
data, so `e.reason` on a `blocked` event without a reason raised AttributeError
— which takes out the whole handler, and a raised handler leaves the robot
uncommanded. One absent key read as a frozen city.
"""
import pytest

from simcode.contract import Event


def blocked(payload: dict) -> Event:
    """The envelope exactly as GAME delivers it."""
    return Event({"city": "c", "type": "event", "event": "blocked", "robot": "r1", "payload": payload})


def test_present_field_is_returned():
    assert blocked({"reason": "level_required"}).reason == "level_required"


def test_missing_public_field_is_none_not_raise():
    # The reporter's repro, verbatim.
    assert blocked({}).reason is None


def test_a_handler_can_compare_a_missing_field_without_dying():
    # The shape CLAUDE.md's own example encourages. This is the whole point:
    # the comparison must be reachable, not the AttributeError.
    e = blocked({})
    assert (e.reason == "level_required") is False


def test_private_and_dunder_still_raise():
    # Python's own attribute protocols (copy, pickle, repr helpers) must keep
    # seeing AttributeError, exactly as _Attr keeps them seeing it — otherwise
    # copy() and friends believe the hook exists and call None.
    # (`__getstate__` is deliberately NOT in this list: object grows one in
    # 3.11+, so it resolves normally and never reaches __getattr__.)
    e = blocked({})
    for name in ("_secret", "__deepcopy__", "__copy__", "__reduce_ex__x"):
        with pytest.raises(AttributeError):
            getattr(e, name)


def test_copy_still_works_because_the_dunders_raise():
    # The reason the rule above matters, stated as behaviour rather than trivia.
    import copy

    e = blocked({"reason": "no_energy"})
    assert copy.copy(e).reason == "no_energy"
    assert copy.deepcopy(e).reason == "no_energy"


def test_robot_alias_and_get_are_unchanged():
    e = blocked({"reason": "no_energy"})
    assert e.robot == "r1" and e.robot_id == "r1"
    assert e.get("reason") == "no_energy"
    assert e.get("nope", "fallback") == "fallback"


def test_a_falsy_payload_value_is_not_confused_with_absence():
    # 0 / "" / False are real values and must come back as themselves, not None.
    e = Event({"event": "x", "payload": {"zero": 0, "empty": "", "no": False}})
    assert e.zero == 0
    assert e.empty == ""
    assert e.no is False
    assert e.missing is None
