import json

from restockbot.matcher import Matcher
from restockbot.config import load_watchlist
from restockbot.models import IN_STOCK, LIMITED, OUT_OF_STOCK, PREORDER, COMING_SOON
from restockbot.sources import target, bestbuy, walmart, shopify, reddit, pokemoncenter, tcgplayer


def matcher():
    return Matcher(load_watchlist(), ["pokemon", "onepiece"])


# ---------------- matcher ----------------
def test_watchlist_matches_specific_products():
    m = matcher()
    pc = m.match("Pokémon TCG: Mega Evolution—Delta Reign Pokémon Center Elite Trainer Box")
    assert pc.watch_id == "dr-pc-etb" and pc.priority == 5 and pc.msrp == 59.99
    etb = m.match("Pokémon Trading Card Game: Mega Evolution—Delta Reign Elite Trainer Box")
    assert etb.watch_id == "dr-etb" and etb.msrp == 49.99
    assert m.match("Pokemon TCG 30th Celebration Ultra-Premium Collection").watch_id == "30th-upc"
    assert m.match("Pokémon TCG: 30th Celebration Mini Tin - Umbreon").watch_id == "30th-other"
    assert m.match("Pokémon Trading Card Game: Deck Crafter's Collection").watch_id == "deck-crafters"
    op = m.match("ONE PIECE Card Game: The Dominance of God Booster Box [OP-18]")
    assert op.watch_id == "op18-box" and op.game == "onepiece"
    assert m.match("One Piece Card Game - Adventure on Kami's Island Booster Pack OP-15").watch_id == "op15"
    # local shops often drop the word "Pokemon"
    assert m.match("Delta Reign Booster Bundle").watch_id == "dr-bundle"


def test_generic_and_exclusions():
    m = matcher()
    g = m.match("Pokémon Trading Card Game: Scarlet & Violet Some Future Set Booster Bundle")
    assert g and not g.watch_id and g.product_type == "Booster Bundle" and g.msrp == 26.94
    assert m.match("Pokemon Pikachu 8\" Plush") is None
    assert m.match("Pokémon Legends Z-A - Nintendo Switch") is None
    assert m.match("Delta Reign Card Sleeves") is None
    assert m.match("One Piece Vol. 105 Manga") is None
    assert m.match("Pokemon Mystery Repack Box 10 packs") is None


def test_price_filter():
    m = matcher()
    etb = m.match("Pokemon TCG Delta Reign Elite Trainer Box")
    assert m.price_ok(49.99, etb) and m.price_ok(54.99, etb)
    assert not m.price_ok(119.99, etb)          # marketplace scalper
    assert m.below_msrp(39.99, etb) and not m.below_msrp(49.99, etb)


# ---------------- Target ----------------
SEARCH = {"data": {"search": {"products": [
    {"tcin": "94300001", "item": {"product_description": {"title": "Pok&#233;mon Trading Card Game: Mega Evolution&#8212;Delta Reign Elite Trainer Box"},
                                  "enrichment": {"buy_url": "https://www.target.com/p/-/A-94300001", "images": {"primary_image_url": "img"}}},
     "price": {"current_retail": 49.99}},
    {"tcin": "94300002", "item": {"product_description": {"title": "Pokemon Delta Reign ETB"}, "fulfillment": {"is_marketplace": True}},
     "price": {"formatted_current_price": "$129.99"}},
]}}}

FIATS = {"data": {"fulfillment_fiats": {"locations": [
    {"location_id": "1375", "location_name": "Midtown", "distance": 2.4, "location_available_to_promise_quantity": 6.0,
     "in_store_only": {"availability_status": "IN_STOCK"}, "order_pickup": {"availability_status": "IN_STOCK"}},
    {"location_id": "2210", "location_name": "Northside", "distance": 11.0, "location_available_to_promise_quantity": 0.0,
     "in_store_only": {"availability_status": "OUT_OF_STOCK"}, "order_pickup": {"availability_status": "UNAVAILABLE"}},
    {"location_id": "3301", "location_name": "Far Away", "distance": 41.0, "location_available_to_promise_quantity": 3.0,
     "in_store_only": {"availability_status": "LIMITED_STOCK"}},
]}}}

PF = {"data": {"product": {"tcin": "94300001", "fulfillment": {
    "shipping_options": {"availability_status": "PRE_ORDER_SELLABLE"},
    "store_options": [{"location_id": "1375", "location_name": "Midtown", "location_available_to_promise_quantity": 6.0,
                       "order_pickup": {"availability_status": "IN_STOCK"}, "in_store_only": {"availability_status": "IN_STOCK"}}]}}}}

NEARBY = {"data": {"nearby_stores": {"stores": [
    {"store_id": "2210", "location_name": "Northside", "distance": 11.0,
     "mailing_address": {"address_line1": "9 Oak Rd", "city": "Springfield", "region": "IL", "postal_code": "62704"}},
    {"store_id": "1375", "location_name": "Midtown", "distance": 2.4,
     "mailing_address": {"address_line1": "1 Main St", "city": "Springfield", "region": "IL", "postal_code": "62701"}},
]}}}


def test_target_parsers():
    items = target.parse_search(SEARCH)
    assert items[0]["title"].startswith("Pokémon Trading Card Game: Mega Evolution—Delta Reign")
    assert items[0]["price"] == 49.99 and not items[0]["marketplace"]
    assert items[1]["marketplace"] and items[1]["price"] == 129.99
    stores = target.parse_nearby_stores(NEARBY)
    assert [s["id"] for s in stores] == ["1375", "2210"] and stores[0]["name"] == "Target Midtown"
    assert stores[0]["address"] == "1 Main St, Springfield, IL"
    rows = target.parse_store_stock(FIATS, {s["id"]: s for s in stores})
    by = {r.store_id: r for r in rows}
    assert by["1375"].status == IN_STOCK and by["1375"].quantity == 6 and by["1375"].pickup
    assert by["2210"].status == OUT_OF_STOCK
    assert by["3301"].status == LIMITED and by["3301"].distance_miles == 41.0
    assert target.parse_online_status(PF) == PREORDER
    assert target.map_status("PRE_ORDER_UNSELLABLE") == COMING_SOON


# ---------------- Best Buy ----------------
def test_bestbuy():
    assert bestbuy.map_orderable({"orderable": "Available", "onlineAvailability": True}) == IN_STOCK
    assert bestbuy.map_orderable({"orderable": "SoldOut"}) == OUT_OF_STOCK
    assert bestbuy.map_orderable({"orderable": "PreOrder"}) == PREORDER
    rows = bestbuy.parse_stores({"stores": [
        {"storeID": "281", "name": "Downtown", "address": "5 Elm", "city": "X", "state": "IL", "distance": 4.2, "lowStock": True},
        {"storeID": "999", "name": "Far", "distance": 80.0}]}, 25)
    assert len(rows) == 1 and rows[0].status == LIMITED and rows[0].name == "Best Buy Downtown"


# ---------------- Walmart ----------------
def test_walmart():
    nd = {"props": {"pageProps": {"initialData": {"searchResult": {"itemStacks": [{"items": [
        {"usItemId": "111", "name": "Pokemon TCG Delta Reign Elite Trainer Box", "canonicalUrl": "/ip/x/111",
         "priceInfo": {"currentPrice": {"price": 49.97}}, "availabilityStatusV2": {"value": "IN_STOCK"}, "sellerName": "Walmart.com"},
        {"usItemId": "222", "name": "Pokemon TCG Delta Reign Elite Trainer Box", "canonicalUrl": "/ip/y/222",
         "priceInfo": {"currentPrice": {"price": 99.0}}, "availabilityStatusV2": {"value": "IN_STOCK"}, "sellerName": "CardFlipperLLC"},
    ]}]}}}}}
    page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>'
    ls = walmart.parse_search_page(page)
    assert len(ls) == 2 and ls[0].url == "https://www.walmart.com/ip/x/111" and ls[0].status == IN_STOCK
    assert not ls[0].third_party and ls[1].third_party


# ---------------- Shopify / Reddit / Pokémon Center / TCGplayer ----------------
def test_shopify():
    data = {"products": [{"id": 7, "title": "Delta Reign Booster Box", "handle": "dr-box",
                          "variants": [{"available": False, "price": "149.99"}, {"available": True, "price": "144.99"}]}]}
    l = shopify.parse_products(data, "shop.example.com", "Example Cards")[0]
    assert l.status == IN_STOCK and l.price == 144.99 and l.url == "https://shop.example.com/products/dr-box"


def test_reddit_feed():
    xml = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>t3_abc</id><title>Delta Reign ETBs at Target in Springfield!</title>
      <link href="https://reddit.com/r/x/abc"/><content type="html">&lt;p&gt;6 on the shelf&lt;/p&gt;</content></entry></feed>"""
    posts = reddit.parse_feed(xml)
    assert posts[0]["id"] == "t3_abc" and posts[0]["body"] == "6 on the shelf"


def test_pokemon_center_detection():
    assert pokemoncenter.detect_queue("https://pokemoncenter.queue-it.net/?c=x", "")
    assert pokemoncenter.detect_queue("https://www.pokemoncenter.com/", "<h1>You are now in line</h1>")
    assert not pokemoncenter.detect_queue("https://www.pokemoncenter.com/", "<h1>Shop</h1>")
    assert pokemoncenter.page_status('<button>Add to Cart</button>') == IN_STOCK
    assert pokemoncenter.page_status('<div>Sold Out</div><button>Add to Cart</button>') == OUT_OF_STOCK


def test_tcgplayer():
    assert tcgplayer.parse_details({"marketPrice": 515.79, "lowestPrice": 480}) == (515.79, 480.0)
    assert tcgplayer.parse_details({"results": [{"marketPrice": 10}]}) == (10.0, None)


def test_target_locations_api_format():
    data = {"locations": [{"location_id": 1176, "location_names": [{"name": "Thornton"}], "distance": 3.1,
                           "address": {"address_line1": "1 St", "city": "Thornton", "region": "CO", "postal_code": "80023"}}]}
    s = target.parse_nearby_stores(data)[0]
    assert s["id"] == "1176" and s["name"] == "Target Thornton" and s["state"] == "CO"


def test_toys_not_matched_by_set_catchall():
    m = matcher()
    assert m.match("Pokémon Battle Fig 30th Anniversary Multipack") is None
    assert m.match("Pokemon 30th Anniversary Plush Pikachu") is None
    assert m.match("Pokémon TCG: 30th Celebration Mini Tin").watch_id == "30th-other"
    assert m.match("30th Celebration Booster Pack").watch_id == "30th-other"


def test_reddit_harvests_target_links(tmp_path):
    from restockbot.db import DB
    from restockbot.sources.base import Context
    from restockbot.config import DEFAULTS
    db = DB(tmp_path / "x.db")
    ctx = Context(DEFAULTS, load_watchlist(), matcher(), db)
    src = reddit.RedditSource({}, None, ctx)
    src.harvest_target_links([
        {"title": "[Target] Delta Reign ETB $49.99", "url": "",
         "raw": '<a href="https://www.target.com/p/pokemon-delta-reign-etb/-/A-94300001">link</a>'},
        {"title": "Pikachu plush at Target", "url": "", "raw": "https://www.target.com/p/x/-/A-11111111"},
    ])
    assert db.get("target_seed_tcins") == {"94300001": "[Target] Delta Reign ETB $49.99"}


def test_target_flow_with_block(tmp_path, monkeypatch):
    import requests
    from restockbot.db import DB
    from restockbot.sources.base import Context
    from restockbot.config import DEFAULTS, deep_merge
    monkeypatch.setattr(target, "ROOT", tmp_path)
    (tmp_path / "target_items.txt").write_text("# x\nhttps://www.target.com/p/a/-/A-55500001\n")
    cfg = deep_merge(DEFAULTS, {"location": {"zip": "80022"}})
    db = DB(tmp_path / "x.db")
    ctx = Context(cfg, load_watchlist(), matcher(), db)
    calls = []

    class FakeHttp:
        s = type("S", (), {"headers": {"User-Agent": "x"}})()

        def get(self, *a, **k):
            raise RuntimeError("offline")

        def json(self, url, params=None, **k):
            calls.append(url.rsplit("/", 1)[-1])
            if "zippopotam" in url:
                return {"places": [{"latitude": "39.9", "longitude": "-104.8", "state abbreviation": "CO"}]}
            if "nearby_stores" in url:
                return NEARBY
            if "plp_search" in url:
                if calls.count("plp_search_v2") > 1:
                    raise requests.HTTPError("435 from redsky.target.com: px")
                return SEARCH
            if "product_fulfillment" in url:
                pf = json.loads(json.dumps(PF))
                pf["data"]["product"]["item"] = {"product_description": {"title": "Pokemon TCG Delta Reign Booster Bundle"}}
                return pf
            if "fiats" in url:
                return FIATS
            raise requests.HTTPError("404")

    monkeypatch.setattr(target.time, "sleep", lambda s: None)
    src = target.TargetSource({"searches_per_run": 4}, FakeHttp(), ctx)
    res = src.check()
    assert calls.count("plp_search_v2") == 2               # stopped after the block
    skus = {l.sku: l for l in res.listings}
    assert "55500001" in skus and skus["55500001"].title == "Pokemon TCG Delta Reign Booster Bundle"
    assert "94300001" in skus and len(skus["94300001"].stores) == 2   # 41-mile store excluded
    assert db.get("target_search_offset") == 1


def test_sitesearch_jsonld_and_links():
    from restockbot.sources import sitesearch
    ld = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "item": {"@type": "Product", "name": "Pokémon TCG Delta Reign Elite Trainer Box 3-Pack",
                                        "url": "/pokemon-etb-3pk.product.4000123456.html",
                                        "offers": {"price": "139.99", "availability": "https://schema.org/InStock"}}},
        {"@type": "ListItem", "item": {"@type": "Product", "name": "Pokemon Mini Tin Bundle",
                                        "url": "https://www.costco.com/tins.product.4000999999.html",
                                        "offers": {"price": "39.99", "availability": "http://schema.org/OutOfStock"}}}]}
    page = f'<html><script type="application/ld+json">{json.dumps(ld)}</script></html>'
    ls = sitesearch.parse_page(page, "https://www.costco.com/CatalogSearch?keyword=pokemon", "costco", "Costco")
    by = {l.sku: l for l in ls}
    assert by["4000123456"].status == IN_STOCK and by["4000123456"].price == 139.99
    assert by["4000123456"].url == "https://www.costco.com/pokemon-etb-3pk.product.4000123456.html"
    assert by["4000999999"].status == OUT_OF_STOCK
    # site with no structured data: product links only
    html_only = '<a href="/p/pokemon-booster-pack-12345678">Pokemon Trading Card Game Booster Pack</a><a href="/x">Hammer</a>'
    ls = sitesearch.parse_page(html_only, "https://www.acehardware.com/search?query=pokemon", "acehardware", "Ace Hardware")
    assert len(ls) == 1 and ls[0].status == "UNKNOWN" and ls[0].sku == "12345678"


def test_sitesearch_next_data():
    from restockbot.sources import sitesearch
    nd = {"props": {"pageProps": {"results": [
        {"productName": "Pokemon TCG Sleeved Booster", "pdpUrl": "/product/pokemon-sleeved-booster/55501",
         "price": {"value": 4.99}, "inventory": {"availabilityStatus": "IN_STOCK"}}]}}}
    page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
    l = sitesearch.parse_page(page, "https://www.fivebelow.com/search?q=pokemon", "fivebelow", "Five Below")[0]
    assert l.title == "Pokemon TCG Sleeved Booster" and l.price == 4.99 and l.status == IN_STOCK


def test_samsclub_walmart_style_page():
    from restockbot.sources import sitesearch
    nd = {"props": {"pageProps": {"initialData": {"searchResult": {"itemStacks": [{"items": [
        {"usItemId": "13579", "name": "Pokémon TCG Prismatic Evolutions Booster Bundle 2-Pack",
         "canonicalUrl": "/ip/pokemon-bundle-2pk/13579", "priceInfo": {"currentPrice": {"price": 52.98}},
         "availabilityStatusV2": {"value": "IN_STOCK"}}]}]}}}}}
    page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
    l = sitesearch.parse_page(page, "https://www.samsclub.com/s/pokemon%20cards", "samsclub", "Sam's Club")[0]
    assert l.url == "https://www.samsclub.com/ip/pokemon-bundle-2pk/13579" and l.status == IN_STOCK


def test_reddit_vending_reports(tmp_path):
    from restockbot.db import DB
    from restockbot.sources.base import Context
    from restockbot.config import DEFAULTS, deep_merge
    cfg = deep_merge(DEFAULTS, {"sources": {"reddit": {"local_terms": ["Thornton"]}}})
    db = DB(tmp_path / "v.db")
    ctx = Context(cfg, load_watchlist(), matcher(), db)

    class Http:
        def __init__(self, xml):
            self.xml = xml

        def get(self, *a, **k):
            return type("R", (), {"text": self.xml})()

    def feed(*entries):
        body = "".join(f'<entry><id>{i}</id><title>{t}</title><link href="https://r/{i}"/><content>x</content></entry>'
                       for i, t in entries)
        return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'
    src = reddit.RedditSource(cfg["sources"]["reddit"], Http(feed(("a", "old post"))), ctx)
    src.check()  # first fetch: quiet
    src.http = Http(feed(("b", "Pokemon vending machine at King Soopers Thornton just restocked!"),
                         ("c", "Vending machine in Ohio restocked with pokemon packs")))
    sig = {s.key: s for s in src.check().signals}
    assert sig["reddit:b"].title.startswith("🎰📍 Local vending") and sig["reddit:b"].priority == 4
    assert sig["reddit:c"].priority == 2
    ev = db.q("SELECT store_name FROM events WHERE kind='vending_report'")
    assert ev[0]["store_name"] == "King Soopers Thornton"


def test_kroger_parsers_and_check(tmp_path):
    from restockbot.sources import kroger
    from restockbot.db import DB
    from restockbot.sources.base import Context
    from restockbot.config import DEFAULTS, deep_merge
    locs = {"data": [
        {"locationId": "62000123", "chain": "KING_SOOPERS", "name": "Thornton Marketplace",
         "address": {"addressLine1": "1 Main", "city": "Thornton", "state": "CO"},
         "geolocation": {"latitude": 39.90, "longitude": -104.97}},
        {"locationId": "62000999", "chain": "KING_SOOPERS", "name": "Far Store",
         "address": {"addressLine1": "9 Rd", "city": "Pueblo", "state": "CO"},
         "geolocation": {"latitude": 38.25, "longitude": -104.6}}]}
    stores = kroger.parse_locations(locs, (39.93, -104.88))
    assert stores[0]["name"] == "King Soopers Thornton Marketplace" and stores[0]["distance"] < 10
    assert stores[1]["distance"] > 100
    prods = {"data": [{"productId": "0082054300", "description": "Pokemon Trading Card Game Booster Bundle",
                       "aisleLocations": [{"description": "Toys"}],
                       "items": [{"price": {"regular": 26.99, "promo": 0}, "inventory": {"stockLevel": "LOW"},
                                  "fulfillment": {"inStore": True}}]}]}
    p = kroger.parse_products(prods)[0]
    assert p["status"] == LIMITED and p["price"] == 26.99 and p["aisle"] == "Toys"

    cfg = deep_merge(DEFAULTS, {"location": {"zip": "80022"},
                                "sources": {"kroger": {"client_id": "a", "client_secret": "b", "terms": ["pokemon cards"]}}})
    db = DB(tmp_path / "k.db")
    db.set("geo:80022", {"lat": "39.93", "lon": "-104.88"})
    ctx = Context(cfg, load_watchlist(), matcher(), db)

    class Http:
        def post(self, *a, **k):
            return type("R", (), {"status_code": 200, "json": lambda s: {"access_token": "t", "expires_in": 1800}})()

        def json(self, url, **k):
            return locs if "locations" in url else prods

    src = kroger.KrogerSource(cfg["sources"]["kroger"], Http(), ctx)
    src.scfg["max_stores"] = 1
    res = src.check()
    l = res.listings[0]
    assert l.retailer == "kroger" and l.stores[0].status == LIMITED and "Toys" in l.stores[0].address
    assert l.stores_checked and l.url.startswith("https://www.kingsoopers.com/")
