"""eBay asking prices via eBay's official Browse API (free key: https://developer.ebay.com).

For a product, it reads current Buy It Now listings that are new and sealed, keeps only listings that
are the same product (same set and same product type, no lots, opened or foreign-language items), and
takes the median price. A store price above that typical eBay asking price never alerts.

Sold prices aren't offered to regular developers (eBay restricts that data), so recent-sales value comes
from the market value check instead.
"""
from __future__ import annotations

import base64
import logging
import statistics
import time
from dataclasses import dataclass
from typing import Optional

from .market import same_product
from .matcher import norm
from .sources.base import to_price

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EXCLUDE = ["lot", "lots", "empty", "opened", "open box", "used", "japanese", "japan", "korean", "chinese",
           "simplified", "thai", "indonesian", "code card", "codes", "case of", "sealed case", "proxy", "custom",
           "replica", "graded", "psa", "bgs", "cgc", "single card", "singles", "read description", "damaged"]


@dataclass
class Asking:
    median: float
    count: int
    low: float


def parse_items(data: dict, title: str, min_similarity: float = 0.5) -> list[float]:
    prices = []
    for it in data.get("itemSummaries", []) or []:
        name = it.get("title") or ""
        n = norm(name)
        if any(f" {w} " in n for w in (norm(x).strip() for x in EXCLUDE)):
            continue
        if same_product(title, name) < min_similarity:
            continue
        p = to_price((it.get("price") or {}).get("value"))
        if p:
            prices.append(p)
    return prices


class EbayLookup:
    def __init__(self, http, db, client_id: str, client_secret: str, refresh_hours: float = 6):
        self.http = http
        self.db = db
        self.cid = client_id
        self.secret = client_secret
        self.refresh = refresh_hours * 3600
        self.token = ""
        self.token_exp = 0.0

    def _token(self) -> str:
        if self.token and time.time() < self.token_exp - 60:
            return self.token
        basic = base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode()
        r = self.http.post(TOKEN_URL, data={"grant_type": "client_credentials",
                                            "scope": "https://api.ebay.com/oauth/api_scope"},
                           headers={"Authorization": f"Basic {basic}",
                                    "Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code != 200:
            raise RuntimeError(f"eBay login failed ({r.status_code}); check the eBay keys")
        j = r.json()
        self.token, self.token_exp = j["access_token"], time.time() + float(j.get("expires_in", 7200))
        return self.token

    def search(self, q: str, filt: str, limit: int = 100) -> dict:
        return self.http.json(SEARCH_URL, check_block=False, params={"q": q, "limit": limit, "filter": filt},
                              headers={"Authorization": f"Bearer {self._token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})

    def asking(self, title: str) -> Optional[Asking]:
        k = f"ebay:{norm(title).strip()}"
        cached = self.db.get(k)
        if cached and time.time() - cached.get("ts", 0) < self.refresh:
            return Asking(**cached["a"]) if cached.get("a") else None
        q = " ".join(w for w in norm(title).split() if w not in ("trading", "card", "game", "tcg"))[:100]
        try:
            data = self.http.json(SEARCH_URL, check_block=False, params={
                "q": q, "limit": 100,
                "filter": "buyingOptions:{FIXED_PRICE},conditions:{NEW},itemLocationCountry:US",
            }, headers={"Authorization": f"Bearer {self._token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
        except Exception as e:  # noqa: BLE001
            log.info("eBay lookup failed for '%s': %s", title[:60], e)
            return Asking(**cached["a"]) if cached and cached.get("a") else None
        prices = parse_items(data, title)
        a = Asking(round(statistics.median(prices), 2), len(prices), min(prices)) if len(prices) >= 3 else None
        self.db.set(k, {"ts": time.time(), "a": a.__dict__ if a else None})
        return a
