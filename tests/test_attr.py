"""_Attr (the read-only attribute bag over spot/production/quest dicts) must read a
missing *public* field as None — never raise — so a controller's defensive
`attr.field or default` over an omitempty wire field (e.g. production.queued at 0)
can't crash an event handler. Private/dunder names must still raise so Python's own
attribute protocols keep working."""

import pytest

from simcode._state import _Attr


def test_present_field_returned():
    assert _Attr({"queued": 3}).queued == 3
    assert _Attr({"active": False}).active is False


def test_missing_public_field_is_none_not_raise():
    a = _Attr({"active": True})           # no "queued" key (omitempty at 0)
    assert a.queued is None
    assert (a.queued or 0) == 0           # the defensive idiom must work


def test_empty_bag_reads_none():
    a = _Attr(None)
    assert a.anything is None
    assert (a.progress or 0) == 0


def test_private_and_dunder_still_raise():
    a = _Attr({})
    with pytest.raises(AttributeError):
        a._not_there
    with pytest.raises(AttributeError):
        a.__wrapped__


def test_get_and_bool_unchanged():
    assert _Attr({"x": 1}).get("x") == 1
    assert _Attr({}).get("x", 9) == 9
    assert bool(_Attr({"x": 1})) is True
    assert bool(_Attr({})) is False
