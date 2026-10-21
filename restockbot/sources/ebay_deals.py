"""eBay listings at or under MSRP, from well-rated sellers only.

For each watched product with a known retail price, it searches eBay's official Browse API for new,
sealed, Buy It Now listings located in the US, and alerts when one costs at or under MSRP *including
shipping*. Underpriced listings of hot products are a common scam, so the seller has to have
strong feedback (99%+ positive and 500+ ratings by default), and lots, opened, graded or
foreign-language items are skipped.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..ebay import EXCLUDE, EbayLookup
from ..market import same_product
from ..matcher import norm
from ..models import CheckResult, Signal
from .base import Source, to_price

log = logging.getLogger(__name__)


def parse_deals(data: dict, product: str, msrp: float, *, min_pct: float, min_score: int,
                max_vs_msrp: float = 1.0, include_shipping: bool = True) -> list[dict]:
    out = []
    excl = [norm(x).strip() for x in EXCLUDE]
    for it in data.get("itemSummaries", []) or []:
        title = it.get("title") or ""
        n = norm(title)
        if any(f" {w} " in n for w in excl) or same_product(product, title) < 0.5:
            continue
        price = to_price((it.get("price") or {}).get("value"))
        if not price:
            continue
        ship = 0.0
        opts = it.get("shippingOptions") or []
        if opts:
            ship = to_price((opts[0].get("shippingCost") or {}).get("value")) or 0.0
        total = price + (ship if include_shipping else 0)
        if total > msrp * max_vs_msrp + 0.01:
            continue
        seller = it.get("seller") or {}
        pct = float(seller.get("feedbackPercentage") or 0)
        score = int(seller.get("feedbackScore") or 0)
        if pct < min_pct or score < min_score:
            continue
        out.append({"id": it.get("itemId") or it.get("legacyItemId") or title, "title": title, "price": price,
                    "ship": ship, "total": round(total, 2), "seller": seller.get("username", "?"),
                    "pct": pct, "score": score, "url": it.get("itemWebUrl", "")})
    return sorted(out, key=lambda d: d["total"])


class EbayDealsSource(Source):
    name = "ebay"
    label = "eBay under MSRP"

    def __init__(self, *a, watchlist: dict, **kw):
        super().__init__(*a, **kw)
        self.watchlist = watchlist
        self.lookup: Optional[EbayLookup] = None

    def products(self) -> list[dict]:
        """Named watchlist products with a retail price, e.g. 'Delta Reign Elite Trainer Box' ($49.99)."""
        games = self.ctx.games
        return [w for w in self.watchlist.get("products", [])
                if w.get("name") and w.get("msrp") and w.get("game", "pokemon") in games and w.get("enabled", True)]

    def check(self) -> CheckResult:
        res = CheckResult()
        if not self.lookup:
            self.lookup = EbayLookup(self.http, self.ctx.db, self.scfg["client_id"], self.scfg["client_secret"])
        for w in self.products():
            prefix = "pokemon" if w.get("game", "pokemon") == "pokemon" else "one piece"
            q = f"{prefix} {w['name']}".replace("Pokémon", "Pokemon")
            cap = w["msrp"] * float(self.scfg.get("max_vs_msrp", 1.0))
            try:
                data = self.lookup.search(q, "buyingOptions:{FIXED_PRICE},conditions:{NEW},itemLocationCountry:US,"
                                             f"price:[1..{cap:.2f}],priceCurrency:USD", limit=50)
            except Exception as e:  # noqa: BLE001
                log.info("ebay '%s': %s", q, e)
                res.complete = False
                continue
            deals = parse_deals(data, w["name"], w["msrp"], min_pct=float(self.scfg.get("min_feedback_percent", 99)),
                                min_score=int(self.scfg.get("min_feedback_score", 500)),
                                max_vs_msrp=float(self.scfg.get("max_vs_msrp", 1.0)),
                                include_shipping=self.scfg.get("include_shipping", True))
            for d in deals[:3]:
                ship = "free shipping" if not d["ship"] else f"+ ${d['ship']:.2f} shipping"
                res.signals.append(Signal(
                    source=self.name, key=f"ebay:{d['id']}",
                    title=f"🏷️ eBay under MSRP: {w['name']} ${d['total']:.2f}",
                    message=(f"{d['title'][:120]}\n${d['price']:.2f} {ship} = ${d['total']:.2f} total "
                             f"(MSRP ${w['msrp']:.2f})\nSold by {d['seller']} "
                             f"({d['pct']:.1f}% positive, {d['score']:,} ratings) · new · Buy It Now · ships from US\n"
                             "Check the photos and description before buying."),
                    url=d["url"], priority=4, tags=["label", "moneybag"], game=w.get("game", "pokemon")))
            time.sleep(1)
        return res

    def doctor(self) -> str:
        res = self.check()
        return (f"OK: checked {len(self.products())} products, {len(res.signals)} listings at or under MSRP "
                f"from well-rated sellers right now")
