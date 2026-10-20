"""Best Buy via the official developer API (free key: https://developer.bestbuy.com).

It reports online stock and which nearby stores have the item for pickup.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote

from ..models import COMING_SOON, IN_STOCK, LIMITED, OUT_OF_STOCK, PREORDER, UNKNOWN, CheckResult, Listing, StoreStock
from .base import Source, to_price

log = logging.getLogger(__name__)

API = "https://api.bestbuy.com/v1"
FIELDS = "sku,name,salePrice,regularPrice,onlineAvailability,inStoreAvailability,orderable,url,releaseDate,image,marketplace"


def map_orderable(p: dict) -> str:
    o = str(p.get("orderable", "")).lower()
    if o == "preorder":
        return PREORDER
    if o == "comingsoon":
        return COMING_SOON
    if o == "available":
        return IN_STOCK if p.get("onlineAvailability", True) else OUT_OF_STOCK
    if o in ("soldout", "notorderable", "specialorder"):
        return OUT_OF_STOCK
    return IN_STOCK if p.get("onlineAvailability") else (OUT_OF_STOCK if "onlineAvailability" in p else UNKNOWN)


def parse_products(data: dict) -> list[dict]:
    return [p for p in data.get("products", []) if p.get("sku") and p.get("name")]


def parse_stores(data: dict, radius: float) -> list[StoreStock]:
    out = []
    for s in data.get("stores", []):
        dist = to_price(s.get("distance"))
        if dist is not None and dist > radius:
            continue
        out.append(StoreStock(
            store_id=str(s.get("storeID") or s.get("storeId")),
            name=f"Best Buy {s.get('name') or s.get('longName') or ''}".strip(),
            status=LIMITED if s.get("lowStock") else IN_STOCK,
            distance_miles=dist,
            address=", ".join(x for x in [s.get("address"), s.get("city"), s.get("state")] if x),
            pickup=True,
        ))
    return out


class BestBuySource(Source):
    name = "bestbuy"
    label = "Best Buy"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.key = self.scfg.get("api_key", "")
        self.catalog: dict[str, dict] = {}
        self.search_at = 0.0

    def _search(self) -> None:
        for term in self.ctx.search_terms:
            words = [w for w in term.replace("'", "").split() if len(w) > 1]
            q = "&".join(f"search={quote(w)}" for w in words)
            try:
                data = self.http.json(f"{API}/products(({q}))", params={
                    "apiKey": self.key, "format": "json", "show": FIELDS, "pageSize": 100})
            except Exception as e:  # noqa: BLE001
                log.warning("bestbuy search '%s' failed: %s", term, e)
                continue
            for p in parse_products(data):
                if self.ctx.matcher.match(p["name"]):
                    self.catalog[str(p["sku"])] = p
            time.sleep(0.5)  # API limit is 5 calls/second
        self.search_at = time.time()

    def check(self) -> CheckResult:
        if not self.key:
            return CheckResult(complete=False)
        if time.time() - self.search_at > 900 or not self.catalog:
            self._search()
        else:
            # refresh availability for known SKUs in one call
            skus = ",".join(list(self.catalog)[:100])
            try:
                data = self.http.json(f"{API}/products(sku in({skus}))", params={
                    "apiKey": self.key, "format": "json", "show": FIELDS, "pageSize": 100})
                for p in parse_products(data):
                    self.catalog[str(p["sku"])] = p
            except Exception as e:  # noqa: BLE001
                log.warning("bestbuy refresh failed: %s", e)
                return CheckResult(complete=False)
        res = CheckResult()
        for sku, p in self.catalog.items():
            m = self.ctx.matcher.match(p["name"])
            stores, checked = [], False
            if m and m.priority >= 3 and self.ctx.zip and p.get("inStoreAvailability", True):
                try:
                    sd = self.http.json(f"{API}/products/{sku}/stores.json",
                                        params={"postalCode": self.ctx.zip, "apiKey": self.key})
                    stores, checked = parse_stores(sd, self.ctx.radius), True
                    time.sleep(0.3)
                except Exception as e:  # noqa: BLE001
                    log.debug("bestbuy stores %s: %s", sku, e)
            res.listings.append(Listing(
                retailer="bestbuy", sku=sku, title=p["name"],
                url=p.get("url") or f"https://www.bestbuy.com/site/{sku}.p",
                status=map_orderable(p), price=to_price(p.get("salePrice")),
                third_party=bool(p.get("marketplace")), image=p.get("image") or "",
                release_date=p.get("releaseDate") or "", stores=stores, stores_checked=checked,
            ))
        return res

    def doctor(self) -> str:
        if not self.key:
            return "SKIPPED: add a free API key from https://developer.bestbuy.com to config.yaml"
        return super().doctor()
