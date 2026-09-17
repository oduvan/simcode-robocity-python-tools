"""forum #34: nested store writes must persist, and a key must be deletable.

Two halves of one report.

`store["jobs"][rid] = {...}` mutated the backing dict in place and never
reached `set_store`, so it read back correctly for the rest of the handler,
looked right in testing, and was gone after a reload. No error either way —
silent data loss.

And the store had no deletion at all, even though GAME's `mergeStore` has
always read a JSON `null` for a key as DELETE. The wire could express it; the
Python side could not ask for it.
"""
from simcode.contract import Accumulator
from simcode._state import StoreProxy


def fresh(initial: dict | None = None):
    """A live store + a proxy over it, wired the way StateReader wires them."""
    live = dict(initial or {})
    acc = Accumulator()
    proxy = StoreProxy(live, acc.set_store)
    acc.watch_store(live)
    return live, acc, proxy


def sent(acc: Accumulator) -> dict:
    """The store payload that would ride out on this event's intents."""
    intents = acc.build_intents("c", "r1")
    for it in intents:
        if it.store:
            return it.store
    return {}


# --- nested writes ---------------------------------------------------------

def test_a_nested_write_is_sent():
    live, acc, store = fresh({"jobs": {}})
    store["jobs"]["r1"] = {"item": "ore", "n": 10}
    assert sent(acc) == {"jobs": {"r1": {"item": "ore", "n": 10}}}


def test_a_deep_nested_write_is_sent():
    live, acc, store = fresh({"a": {"b": {"c": [1, 2]}}})
    store["a"]["b"]["c"].append(3)
    assert sent(acc) == {"a": {"b": {"c": [1, 2, 3]}}}


def test_a_nested_delete_is_sent_as_the_whole_key():
    # GAME merges by top-level key, so removing one job sends the new `jobs`.
    live, acc, store = fresh({"jobs": {"r1": 1, "r2": 2}})
    del store["jobs"]["r1"]
    assert sent(acc) == {"jobs": {"r2": 2}}


def test_reading_a_nested_value_sends_nothing():
    # mergeStore marks EVERY written key as changed, so a key that was only read
    # must not be sent — or it lands in the delta on every single tick.
    live, acc, store = fresh({"jobs": {"r1": 1}, "phase": "explore"})
    _ = store["jobs"]["r1"]
    _ = store["phase"]
    assert sent(acc) == {}
    assert acc.is_empty()


def test_writing_a_value_back_unchanged_sends_nothing():
    live, acc, store = fresh({"jobs": {"r1": 1}})
    store["jobs"]["r1"] = 1  # same value
    assert sent(acc) == {}


def test_an_explicit_write_wins_over_the_fingerprint():
    live, acc, store = fresh({"phase": "explore"})
    store["phase"] = "mine"
    assert sent(acc) == {"phase": "mine"}


def test_a_nested_write_alone_still_produces_an_intent():
    # Nothing else happened this event: the store change must still get out.
    live, acc, store = fresh({"jobs": {}})
    store["jobs"]["r1"] = 1
    assert not acc.is_empty()
    assert sent(acc) == {"jobs": {"r1": 1}}


# --- deletion --------------------------------------------------------------

def test_del_sends_null_and_removes_it_locally():
    live, acc, store = fresh({"phase": "explore", "jobs": {}})
    del store["phase"]
    assert sent(acc)["phase"] is None   # null == delete, per mergeStore
    assert "phase" not in live
    assert "phase" not in store


def test_pop_returns_the_value_and_deletes():
    live, acc, store = fresh({"phase": "explore"})
    assert store.pop("phase") == "explore"
    assert sent(acc) == {"phase": None}


def test_pop_with_a_default_on_a_missing_key_sends_nothing():
    live, acc, store = fresh({})
    assert store.pop("nope", "fallback") == "fallback"
    assert sent(acc) == {}


def test_pop_without_a_default_raises_like_a_dict():
    live, acc, store = fresh({})
    try:
        store.pop("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("pop on a missing key must raise KeyError")


def test_clear_deletes_every_key():
    live, acc, store = fresh({"a": 1, "b": 2})
    store.clear()
    assert sent(acc) == {"a": None, "b": None}
    assert len(live) == 0


def test_update_writes_each_key():
    live, acc, store = fresh({})
    store.update({"a": 1}, b=2)
    assert sent(acc) == {"a": 1, "b": 2}
    assert live == {"a": 1, "b": 2}


def test_deleting_then_setting_sends_the_new_value_not_the_null():
    live, acc, store = fresh({"phase": "explore"})
    del store["phase"]
    store["phase"] = "mine"
    assert sent(acc) == {"phase": "mine"}
