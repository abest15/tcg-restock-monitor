"""Kroger family stores (King Soopers, Kroger, Fred Meyer, Ralphs, Smith's, Fry's, City Market...)
via Kroger's official developer API (free: https://developer.kroger.com).

For each nearby store it searches for card products and reads that store's stock level
(HIGH / LOW / TEMPORARILY_OUT_OF_STOCK), so restocks at the grocery store show up as in-store alerts.
"""
from __future__ import annotations

import base64
import logging
import math
import time
from typing import Optional

from ..models import IN_STOCK, LIMITED, OUT_OF_STOCK, UNKNOWN, CheckResult, Listing, StoreStock
from .base import Source, clean, dig, to_price

log = logging.getLogger(__name__)

API = "https://api.kroger.com/v1"
STOCK = {"HIGH": IN_STOCK, "LOW": LIMITED, "TEMPORARILY_OUT_OF_STOCK": OUT_OF_STOCK}


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    (la1, lo1), (la2, lo2) = [(math.radians(x), math.radians(y)) for x, y in (a, b)]
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def parse_locations(data: dict, home: Optional[tuple[float, float]]) -> list[dict]:
    out = []
    for d in data.get("data", []) or []:
        addr = d.get("address") or {}
        geo = d.get("geolocation") or {}
        dist = None
        if home and geo.get("latitude") is not None:
            dist = round(miles(home, (float(geo["latitude"]), float(geo["longitude"]))), 1)
        chain = clean(d.get("chain") or "Kroger").replace("_", " ").title()
        out.append({"id": str(d.get("locationId")), "name": f"{chain} {clean(d.get('name'))}".strip(),
                    "address": ", ".join(x for x in [addr.get("addressLine1"), addr.get("city"), addr.get("state")] if x),
                    "distance": dist, "chain": chain})
    return out


def parse_products(data: dict) -> list[dict]:
    out = []
    for p in data.get("data", []) or []:
        item = (p.get("items") or [{}])[0] or {}
        level = dig(item, "inventory", "stockLevel")
        status = STOCK.get(str(level or "").upper())
        if status is None:
            status = IN_STOCK if dig(item, "fulfillment", "inStore") else UNKNOWN
        price = to_price(dig(item, "price", "promo")) or to_price(dig(item, "price", "regular"))
        out.append({"id": str(p.get("productId")), "title": clean(p.get("description")), "status": status,
                    "price": price, "aisle": clean(dig(p, "aisleLocations", 0, "description", default=""))})
    return out


class KrogerSource(Source):
    name = "kroger"
    label = "King Soopers / Kroger"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.token = ""
        self.token_exp = 0.0
        self.locations: list[dict] = []
        self.loc_at = 0.0

    def _token(self) -> str:
        if self.token and time.time() < self.token_exp - 60:
            return self.token
        cid, secret = self.scfg.get("client_id", ""), self.scfg.get("client_secret", "")
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        r = self.http.post(f"{API}/connect/oauth2/token", data={"grant_type": "client_credentials",
                                                                "scope": "product.compact"},
                           headers={"Authorization": f"Basic {basic}",
                                    "Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code != 200:
            raise RuntimeError(f"Kroger login failed ({r.status_code}); check client_id/client_secret")
        j = r.json()
        self.token, self.token_exp = j["access_token"], time.time() + float(j.get("expires_in", 1800))
        return self.token

    def _get(self, path: str, params: dict) -> dict:
        return self.http.json(f"{API}/{path}", params=params, check_block=False,
                              headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"})

    def stores(self) -> list[dict]:
        if self.locations and time.time() - self.loc_at < 86400:
            return self.locations
        home = None
        geo = self.ctx.db.get(f"geo:{self.ctx.zip}")
        if not geo:
            try:
                place = self.http.json(f"https://api.zippopotam.us/us/{self.ctx.zip}", check_block=False)["places"][0]
                geo = {"lat": place["latitude"], "lon": place["longitude"], "state": place["state abbreviation"]}
                self.ctx.db.set(f"geo:{self.ctx.zip}", geo)
            except Exception as e:  # noqa: BLE001
                log.info("zip lookup failed: %s", e)
        if geo:
            home = (float(geo["lat"]), float(geo["lon"]))
        data = self._get("locations", {"filter.zipCode.near": self.ctx.zip,
                                       "filter.radiusInMiles": int(min(self.ctx.radius, 100)), "filter.limit": 20})
        locs = parse_locations(data, home)
        locs.sort(key=lambda s: s["distance"] if s["distance"] is not None else 999)
        self.locations = locs[: int(self.scfg.get("max_stores", 8))]
        self.loc_at = time.time()
        return self.locations

    def terms(self) -> list[str]:
        return self.scfg.get("terms") or ([] + (["pokemon cards"] if "pokemon" in self.ctx.games else [])
                                          + (["one piece card game"] if "onepiece" in self.ctx.games else []))

    def check(self) -> CheckResult:
        res = CheckResult()
        if not (self.scfg.get("client_id") and self.scfg.get("client_secret") and self.ctx.zip):
            res.complete = False
            return res
        products: dict[str, Listing] = {}
        all_ok = True
        for store in self.stores():
            for term in self.terms():
                try:
                    data = self._get("products", {"filter.term": term, "filter.locationId": store["id"],
                                                  "filter.limit": 50})
                except Exception as e:  # noqa: BLE001
                    log.info("kroger %s '%s': %s", store["name"], term, e)
                    all_ok = False
                    continue
                for p in parse_products(data):
                    l = products.setdefault(p["id"], Listing(
                        retailer="kroger", sku=p["id"], title=p["title"],
                        url=f"https://www.kingsoopers.com/p/-/{p['id']}" if "Soopers" in store["chain"]
                        else f"https://www.kroger.com/p/-/{p['id']}",
                        status=UNKNOWN, price=p["price"], seller=store["chain"]))
                    l.stores.append(StoreStock(store_id=store["id"], name=store["name"], status=p["status"],
                                               distance_miles=store["distance"],
                                               address=store["address"] + (f" · {p['aisle']}" if p["aisle"] else "")))
                time.sleep(0.4)
        for l in products.values():
            l.stores_checked = all_ok
            res.listings.append(l)
        res.complete = all_ok
        return res

    def doctor(self) -> str:
        if not (self.scfg.get("client_id") and self.scfg.get("client_secret")):
            return ("SKIPPED: free keys from https://developer.kroger.com add King Soopers in-store stock "
                    "(run: python -m restockbot keys)")
        stores = self.stores()
        res = self.check()
        cards = sum(1 for l in res.listings if self.ctx.matcher.match(l.title))
        instock = sum(1 for l in res.listings for s in l.stores if s.status in (IN_STOCK, LIMITED))
        return (f"OK: {len(stores)} stores ({', '.join(s['name'] for s in stores[:3])}) | {cards} card products, "
                f"{instock} in stock at a store")
