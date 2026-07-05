"""Base quests & leveling (the game objective). questFor escalates the required
ore+metal geometrically; the Base consumes the requirement from its store and
levels up (possibly multiple levels in one tick), emitting base_level_up +
quest_updated. Mirror of the Go engine (buildings.go advanceBaseQuest)."""

from robocity_sim.config import default_config
from robocity_sim.module import (
    Module, EVENT_BASE_LEVEL_UP, EVENT_QUEST_UPDATED,
)


def test_quest_for_geometric_schedule():
    cfg = default_config()
    assert cfg.quest_for(0) == (40, 20)  # < 1 treated as level 1
    assert cfg.quest_for(1) == (40, 20)
    assert cfg.quest_for(2) == (60, 30)
    assert cfg.quest_for(3) == (90, 45)
    assert cfg.quest_for(4) == (135, 67)  # integer math: 90*3//2, 45*3//2


def test_initial_quest_announced_once():
    m = Module(default_config())
    m.reset_world("t", 7)
    evs = m.advance(1)
    updated = [e for e in evs if e["event"] == EVENT_QUEST_UPDATED]
    assert len(updated) == 1, "initial quest should be announced exactly once"
    assert updated[0]["payload"] == {"level": 1, "requirements": {"ore": 40, "metal": 20}}
    # Not re-announced on later ticks with no progress.
    evs2 = m.advance(2)
    assert not [e for e in evs2 if e["event"] == EVENT_QUEST_UPDATED]


def test_base_levels_up_and_consumes_store():
    m = Module(default_config())
    m.reset_world("t", 7)
    b = m.wd.base()
    b.ore, b.metal = 40, 20  # exactly enough for level 1 -> 2
    evs = m.advance(1)
    assert b.level == 2
    assert b.ore == 0 and b.metal == 0  # requirement consumed
    lvlups = [e for e in evs if e["event"] == EVENT_BASE_LEVEL_UP]
    assert len(lvlups) == 1
    assert lvlups[0]["payload"] == {"level": 2, "quest": {"ore": 60, "metal": 30}}


def test_big_delivery_levels_up_multiple_times():
    m = Module(default_config())
    m.reset_world("t", 7)
    b = m.wd.base()
    # L1=40/20, L2=60/30 -> 100/50 clears two levels, leaving 0/0.
    b.ore, b.metal = 100, 50
    evs = m.advance(1)
    assert b.level == 3
    assert b.ore == 0 and b.metal == 0
    assert len([e for e in evs if e["event"] == EVENT_BASE_LEVEL_UP]) == 2


def test_objective_string_tracks_level():
    m = Module(default_config())
    m.reset_world("t", 7)
    assert m.objective() == "⭐ Base level 1 — next: 40 ore + 20 metal"
    st = m.build_state(1, 1)
    assert st["objective"] == m.objective()
    base_form = next(b for b in st["buildings"] if b["type"] == "base")
    assert base_form["level"] == 1
    assert base_form["quest"]["required"] == {"ore": 40, "metal": 20}
