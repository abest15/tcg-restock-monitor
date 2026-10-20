"""Local card shops and hobby stores running Shopify. Most do, and every Shopify store
publishes /products.json, so new products and restocks can be checked reliably."""
from __future__ import annotations

import logging
import time

from ..models import IN_STOCK, OUT_OF_STOCK, CheckResult, Listing
from .base import Source, clean, to_price

log = logging.getLogger(__name__)


def parse_products(data: dict, domain: str, shop_name: str) -> list[Listing]:
    out = []
    for p in data.get("products", []):
        variants = p.get("variants") or []
        available = any(v.get("available") for v in variants)
        prices = [to_price(v.get("price")) for v in variants if to_price(v.get("price"))]
        out.append(Listing(
            retailer=f"shop:{domain}", sku=str(p.get("id")), title=clean(p.get("title")),
            url=f"https://{domain}/products/{p.get('handle')}",
            status=IN_STOCK if available else OUT_OF_STOCK,
            price=min(prices) if prices else None, seller=shop_name,
            image=((p.get("images") or [{}])[0] or {}).get("src", ""),
        ))
    return out


class ShopifySource(Source):
    name = "shopify"
    label = "Card shops"

    def __init__(self, *a, shops: list | None = None, **kw):
        super().__init__(*a, **kw)
        self.extra = shops or []      # online shops listed in watchlist.yaml (kept current by auto-update)
        self.primed: set[str] = set()

    def shops(self) -> list[dict]:
        out, seen = [], set()
        for shop in list(self.scfg.get("stores", []) or []) + list(self.extra):
            if isinstance(shop, str):
                shop = {"domain": shop}
            domain = shop["domain"].replace("https://", "").replace("http://", "").split("/")[0]
            if domain not in seen:
                seen.add(domain)
                out.append({**shop, "domain": domain})
        return out

    def check(self) -> CheckResult:
        res = CheckResult()
        self.stats: list[str] = []
        for shop in self.shops():
            domain = shop["domain"]
            first = domain not in self.primed
            n = 0
            failed = False
            for page in range(1, int(shop.get("pages", 3)) + 1):
                try:
                    data = self.http.json(f"https://{domain}/products.json",
                                          params={"limit": 250, "page": page}, check_block=False)
                except Exception as e:  # noqa: BLE001
                    log.info("shopify %s: %s", domain, e)
                    res.complete = False
                    failed = True
                    self.stats.append(f"{shop.get('name', domain)}: FAILED {str(e)[:60]}")
                    break
                listings = parse_products(data, domain, shop.get("name", domain))
                for l in listings:
                    l.new_discovery = not first  # first successful read of a shop = what it already had
                res.listings += listings
                n += len(listings)
                if len(listings) < 250:
                    break
                time.sleep(1)
            if not failed:
                self.primed.add(domain)
                self.stats.append(f"{shop.get('name', domain)}: {n} products")
            time.sleep(2)
        return res

    def doctor(self) -> str:
        res = self.check()
        cards = sum(1 for l in res.listings if self.ctx.matcher.match(l.title))
        return f"{cards} card products | " + " | ".join(getattr(self, "stats", [])) if self.shops() else "no shops configured"
