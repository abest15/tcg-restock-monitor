import pytest

from restockbot import market
from restockbot.config import DEFAULTS, deep_merge
from restockbot.matcher import Match
from restockbot.models import IN_STOCK, OUT_OF_STOCK, Listing, StoreStock


def test_same_product_matching():
    sp = market.same_product
    r = "Pokémon Trading Card Game: Mega Evolution—Delta Reign Elite Trainer Box"
    assert sp(r, "ME06: Delta Reign Elite Trainer Box") >= 0.5
    assert sp(r, "ME06: Delta Reign Booster Bundle") == 0                       # different product type
    assert sp(r, "ME06: Delta Reign Pokemon Center Elite Trainer Box") == 0     # PC exclusive is different
    assert sp(r, "30th Celebration Elite Trainer Box") < 0.5                    # different set
    assert sp("ONE PIECE Card Game The Dominance of God Booster Box OP-18",
              "The Dominance of God Booster Box [OP-18]") >= 0.5


def test_parse_search():
    data = {"results": [{"results": [
        {"productId": 712100.0, "productName": "Delta Reign Elite Trainer Box", "marketPrice": 89.5, "lowestPrice": 84.0,
         "lowestPriceWithShipping": 90.0}]}]}
    assert market.parse_search(data) == [{"id": 712100, "name": "Delta Reign Elite Trainer Box",
                                          "market": 89.5, "lowest": 84.0}]


class FakeLookup:
    def __init__(self, m):
        self.m = m

    def find(self, title, game, known_id=None):
        return self.m


def gate(m, **opts):
    cfg = deep_merge(DEFAULTS, {"alerts": {"deal_filter": opts}})
    return market.PriceGate(cfg, FakeLookup(m))


M = Match(game="pokemon", product_type="Elite Trainer Box", msrp=49.99, priority=5)


def test_gate_rules():
    mk = market.Market(1, "Delta Reign ETB", market=89.50, lowest=84.00)
    ok, line = gate(mk).check("x", 49.99, M)
    assert ok and "$34.01 below TCGplayer" in line                 # beats the lower of market/lowest
    assert not gate(mk).check("x", 86.00, M)[0]                    # under market but above lowest listing
    assert gate(mk, compare="market").check("x", 86.00, M)[0]
    assert not gate(None).check("x", 49.99, M)[0]                  # unknown -> skip (strict)
    assert gate(None, when_no_tcgplayer_price="alert").check("x", 49.99, M)[0]
    assert not gate(mk).check("x", None, M)[0]                     # no store price -> can't compare


def test_engine_only_alerts_on_deals(tmp_path):
    from restockbot.config import load_watchlist
    from restockbot.db import DB
    from restockbot.engine import Engine
    from tests.test_engine import Capture, FakeSource, run, listing, store
    cfg = deep_merge(DEFAULTS, {"location": {"zip": "80022", "radius_miles": 25}})
    wl = load_watchlist()
    from restockbot.matcher import Matcher
    n = Capture(cfg)
    src = FakeSource()
    eng = Engine(cfg, wl, DB(tmp_path / "e.db"), Matcher(wl, cfg["games"]), n, [src])
    mk = market.Market(1, "Delta Reign ETB", market=89.50, lowest=84.00)
    eng.gate = market.PriceGate(cfg, FakeLookup(mk))
    run(eng, src, listing(OUT_OF_STOCK, price=49.99, stores=[store("1")]))
    run(eng, src, listing(IN_STOCK, price=49.99, stores=[store("1", IN_STOCK, 4)]))
    titles = [t for t, _, _ in n.sent]
    assert any("RESTOCK" in t for t in titles) and any("IN-STORE" in t for t in titles)
    assert "below TCGplayer" in n.sent[-1][1]
    # same product priced above TCGplayer at another retailer: silent
    before = len(n.sent)
    run(eng, src, listing(OUT_OF_STOCK, price=99.99, sku="2"))
    run(eng, src, listing(IN_STOCK, price=99.99, sku="2"))
    assert len(n.sent) == before


def test_retail_preferred(tmp_path):
    from restockbot.config import load_watchlist
    from restockbot.db import DB
    from restockbot.engine import Engine
    from restockbot.matcher import Matcher
    from tests.test_engine import Capture, FakeSource, run, listing
    cfg = deep_merge(DEFAULTS, {"location": {"zip": "80022"}})
    wl = load_watchlist()
    n = Capture(cfg)
    src = FakeSource()
    eng = Engine(cfg, wl, DB(tmp_path / "r.db"), Matcher(wl, cfg["games"]), n, [src])
    eng.gate = market.PriceGate(cfg, FakeLookup(market.Market(1, "ETB", market=120.0, lowest=110.0)))
    run(eng, src, listing(OUT_OF_STOCK, price=49.99), listing(OUT_OF_STOCK, price=79.99, sku="2"))
    run(eng, src, listing(IN_STOCK, price=49.99), listing(IN_STOCK, price=79.99, sku="2"))
    retail = next(x for x in n.sent if "RETAIL PRICE" in x[1])
    above = next(x for x in n.sent if "Above retail" in x[1])
    assert retail[2]["priority"] == 5 and above[2]["priority"] == 4
    # retail-only mode drops the above-retail alert
    eng2 = Engine(deep_merge(cfg, {"alerts": {"deal_filter": {"retail_only": True}}}), wl, DB(tmp_path / "r2.db"),
                  Matcher(wl, cfg["games"]), Capture(cfg), [src])
    eng2.gate = market.PriceGate(eng2.cfg, FakeLookup(market.Market(1, "ETB", market=120.0, lowest=110.0)))
    run(eng2, src, listing(OUT_OF_STOCK, price=79.99, sku="3"))
    run(eng2, src, listing(IN_STOCK, price=79.99, sku="3"))
    assert not any("RESTOCK" in t for t, _, _ in eng2.n.sent)


def test_gate_falls_back_to_msrp_when_tcgplayer_down():
    class Down:
        failures = 5

        def find(self, *a, **k):
            return None
    cfg = deep_merge(DEFAULTS, {})
    g = market.PriceGate(cfg, Down())
    ok, line = g.check("x", 49.99, M)
    assert ok and "TCGplayer unavailable" in line
    assert not g.check("x", 129.99, M)[0]
