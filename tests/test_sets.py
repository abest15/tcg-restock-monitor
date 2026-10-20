from datetime import date

import pytest

from restockbot.config import DEFAULTS, deep_merge, load_watchlist
from restockbot.db import DB
from restockbot.matcher import Matcher
from restockbot.sources.base import Context
from restockbot.sources import sets


@pytest.fixture
def ctx(tmp_path):
    cfg = deep_merge(DEFAULTS, {"location": {"zip": "80022"}})
    wl = load_watchlist()
    c = Context(cfg, wl, Matcher(wl, cfg["games"]), DB(tmp_path / "s.db"))
    c.tracker = sets.SetTracker(c, wl)
    return c


def test_parse_pokemon_api():
    data = {"data": [{"id": "me7", "name": "Storm Crown", "series": "Mega Evolution", "releaseDate": "2027/01/29"},
                     {"id": "sv1", "name": "Scarlet & Violet", "series": "Scarlet & Violet", "releaseDate": "2023/03/31"}]}
    s = sets.parse_pokemon_sets(data)
    assert s[0]["name"] == "Storm Crown" and s[0]["date"] == date(2027, 1, 29)


def test_parse_onepiece_products():
    page = """<li><a>BOOSTER PACK -THE DOMINANCE OF GOD- [OP-18]</a></li>
              <li>EXTRA BOOSTER -ONE PIECE HEROINES EDITION VOL.2- [EB-05]</li>
              <li>STARTER DECK -EGGHEAD- [ST-29]</li>"""
    ps = sets.parse_onepiece_products(page)
    assert [p["code"] for p in ps] == ["OP-18", "EB-05", "ST-29"]
    assert ps[0]["name"] == "The Dominance Of God"


def test_set_from_title():
    assert sets.set_from_title("Pokémon Trading Card Game: Mega Evolution—Storm Crown Elite Trainer Box") == "Storm Crown"
    assert sets.set_from_title("Pokemon TCG Storm Crown Booster Bundle") == "Storm Crown"
    assert sets.set_from_title("Pokémon TCG: Pokémon Center Elite Trainer Box") is None
    assert sets.set_from_title("Pokemon Pikachu Plush") is None


def test_new_set_is_searched_prioritized_and_calendared(ctx):
    tr = ctx.tracker
    new = tr.add({"game": "pokemon", "name": "Storm Crown", "date": date(2027, 1, 29)}, "pokemontcg.io")
    assert new and "pokemon storm crown" in ctx.search_terms
    m = ctx.matcher.match("Pokémon TCG: Mega Evolution—Storm Crown Booster Bundle")
    assert m.watch_id == "auto:storm-crown" and m.priority == 4
    assert any("Storm Crown" in r["name"] for r in tr.watchlist["releases"])
    # already-known sets (in the hand-written watchlist) are not re-added
    assert tr.add({"game": "pokemon", "name": "Delta Reign", "date": None}, "x") is None
    # second add of the same set is not "new"
    assert tr.add({"game": "pokemon", "name": "Storm Crown", "date": None}, "x") is None


def test_learn_from_titles_needs_two_products(ctx):
    tr = ctx.tracker
    assert tr.learn_from_title("Pokemon TCG: Mega Evolution—Storm Crown Elite Trainer Box", "pokemon") is None
    got = tr.learn_from_title("Pokemon TCG: Mega Evolution—Storm Crown Booster Bundle", "pokemon")
    assert got and got["name"] == "Storm Crown"


def test_sets_source_first_run_silent_then_alerts(ctx, monkeypatch):
    class FakeHttp:
        calls = 0
        def json(self, *a, **k):
            FakeHttp.calls += 1
            names = [{"id": "me6", "name": "Delta Reign", "releaseDate": "2026/11/06"}]
            if FakeHttp.calls > 1:
                names.append({"id": "me7", "name": "Storm Crown", "releaseDate": "2027/01/29"})
            return {"data": names}
        def get(self, *a, **k):
            return type("R", (), {"text": "<li>BOOSTER PACK -THE DOMINANCE OF GOD- [OP-18]</li>"})()
    src = sets.SetsSource({}, FakeHttp(), ctx, tracker=ctx.tracker)
    assert src.check().signals == []                    # first run: learn quietly
    sig = src.check().signals
    assert len(sig) == 1 and "Storm Crown" in sig[0].title
