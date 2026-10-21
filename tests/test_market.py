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
    cfg = deep_merge(DEFAULTS, {"alerts": {"price_rules": opts}})
    return market.PriceGate(cfg, FakeLookup(m))


M = Match(game="pokemon", product_type="Elite Trainer Box", msrp=49.99, priority=5)
SMALL = Match(game="pokemon", product_type="Blister", msrp=9.99, priority=3)
UPC = Match(game="pokemon", product_type="Ultra-Premium Collection", msrp=179.99, priority=5)


def test_retail_rules():
    hot = market.Market(1, "ETB", market=95.0, lowest=90.0)
    ok, line = gate(hot).check("x", 49.99, M)
    assert ok and line.startswith("✅ RETAIL PRICE")
    assert gate(hot).check("x", 59.99, M)[0]                       # $10 over on a $50 item: allowed
    assert not gate(hot).check("x", 64.99, M)[0]                   # $15 over: scalper territory
    assert not gate(hot).check("x", 89.99, M)[0]                   # below TCGplayer but way over retail: NO
    assert gate(None).check("x", 12.99, SMALL)[0]                  # small item, $3 over
    assert not gate(None).check("x", 14.99, SMALL)[0]              # small item, $5 over
    assert gate(None).check("x", 197.99, UPC)[0]                   # big item, 10% over
    assert not gate(None).check("x", 199.99, UPC)[0]
    assert gate(None, retail_only=True).check("x", 49.99, M)[0]
    assert not gate(None, retail_only=True).check("x", 52.99, M)[0]


def test_never_above_market_value():
    cold = market.Market(1, "Old ETB", market=38.0, lowest=35.0)  # worth less than retail
    ok, line = gate(cold).check("x", 49.99, M)
    assert not ok and "market value $38.00" in line
    assert gate(cold).check("x", 36.99, M)[0]                      # on sale below its value: fine


def test_unknown_retail_or_price_is_skipped():
    unknown = Match(game="pokemon", product_type="Sealed product", msrp=None, priority=3)
    assert not gate(None).check("x", 19.99, unknown)[0]
    assert not gate(None).check("x", None, M)[0]


def test_warehouse_bundles_judged_per_item():
    assert market.bundle_count("Pokemon TCG Delta Reign Elite Trainer Box 3-Pack") == 3
    assert market.bundle_count("Pokemon TCG 3-Pack Blister") == 1
    assert gate(None).check("Delta Reign Elite Trainer Box 3-Pack", 149.99, M, trusted=True)[0]   # $50 each
    assert not gate(None).check("Delta Reign Elite Trainer Box 3-Pack", 229.99, M, trusted=True)[0]
    assert not gate(None).check("Delta Reign Elite Trainer Box", 149.99, M, trusted=True)[0]      # no count: single


def test_engine_only_alerts_at_retail(tmp_path):
    from restockbot.config import load_watchlist
    from restockbot.db import DB
    from restockbot.engine import Engine
    from restockbot.matcher import Matcher
    from tests.test_engine import Capture, FakeSource, run, listing, store
    cfg = deep_merge(DEFAULTS, {"location": {"zip": "80022", "radius_miles": 25}})
    wl = load_watchlist()
    n = Capture(cfg)
    src = FakeSource()
    eng = Engine(cfg, wl, DB(tmp_path / "e.db"), Matcher(wl, cfg["games"]), n, [src])
    eng.gate = market.PriceGate(cfg, FakeLookup(market.Market(1, "ETB", market=95.0, lowest=90.0)))
    run(eng, src, listing(OUT_OF_STOCK, price=49.99, stores=[store("1")]),
        listing(OUT_OF_STOCK, price=84.99, sku="2", stores=[store("1")]))
    run(eng, src, listing(IN_STOCK, price=49.99, stores=[store("1", IN_STOCK, 4)]),
        listing(IN_STOCK, price=84.99, sku="2", stores=[store("1", IN_STOCK, 4)]))
    bodies = [b for _, b, _ in n.sent]
    assert any("RETAIL PRICE" in b for b in bodies)
    assert not any("$84.99" in b for b in bodies)                  # reseller-priced listing never alerts
