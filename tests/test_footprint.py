"""Multi-cell building footprints (#6) — mirror of the server engine's
footprint_test.go. A 2x2 Storage occupies all four cells; placement rejects an
overlap; a 1x1 building is unchanged; the wire form carries w,h.
"""

from robocity_sim.config import BUILDING_FLYING_STATION, BUILDING_STORAGE, default_config
from robocity_sim.module import CMD_BUILD, Intent, Module
from robocity_sim.world import Building, World


def _world():
    wd = World(default_config())
    wd.generate("test", 7)
    return wd


def test_storage_occupies_four_cells_and_clears_on_removal():
    wd = _world()
    st = Building(id="store-1", typ=BUILDING_STORAGE, pos=(5, 5),
                  status="active", has_storage=True, cap=500)
    wd.add_building(st)

    assert (st.w, st.h) == (2, 2)
    for c in [(5, 5), (6, 5), (5, 6), (6, 6)]:
        b = wd.building_at(*c)
        assert b is not None and b.id == "store-1", c
    # A cell just outside the footprint is free.
    assert wd.building_at(7, 5) is None

    wd.remove_building("store-1")
    for c in [(5, 5), (6, 5), (5, 6), (6, 6)]:
        assert wd.building_at(*c) is None, c


def test_default_building_is_single_cell():
    wd = _world()
    fs = Building(id="fs-1", typ=BUILDING_FLYING_STATION, pos=(3, 3), status="active")
    wd.add_building(fs)
    assert (fs.w, fs.h) == (1, 1)
    assert wd.building_at(3, 3) is not None
    for c in [(4, 3), (3, 4), (4, 4)]:
        assert wd.building_at(*c) is None, c


def test_placement_rejects_overlap():
    m = Module(default_config())
    m.reset_world("test", 7)
    wd = m.wd
    wd.add_building(Building(id="store-1", typ=BUILDING_STORAGE, pos=(5, 5),
                             status="active", has_storage=True, cap=500))
    before = len(wd.buildings)
    # A storage anchored at (6,6) covers (6,6)-(7,7), overlapping (6,6).
    evs = m.submit(Intent("", [{"cmd": CMD_BUILD, "args": [BUILDING_STORAGE, 6, 6]}], []), 1)
    assert len(wd.buildings) == before  # nothing placed
    assert any(e.get("event") == "blocked"
               and e.get("payload", {}).get("reason") == "cell_occupied" for e in evs)


def test_form_reports_wh():
    m = Module(default_config())
    m.reset_world("test", 7)
    st = Building(id="store-1", typ=BUILDING_STORAGE, pos=(5, 5),
                  status="active", has_storage=True, cap=500)
    m.wd.add_building(st)
    assert (m._building_form(st)["w"], m._building_form(st)["h"]) == (2, 2)
    base = m.wd.base()
    assert (m._building_form(base)["w"], m._building_form(base)["h"]) == (1, 1)
