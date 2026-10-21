"""Walmart search results (online). Marketplace sellers are flagged so scalper prices are ignored."""
from __future__ import annotations

import json
import logging
import random
import time

from ..models import COMING_SOON, IN_STOCK, LIMITED, OUT_OF_STOCK, PREORDER, UNKNOWN, CheckResult, Listing
from .base import Source, clean, dig, extract_json_script, to_price, walk

log = logging.getLogger(__name__)

STATUS = {"IN_STOCK": IN_STOCK, "OUT_OF_STOCK": OUT_OF_STOCK, "LIMITED_STOCK": LIMITED,
          "PRE_ORDER": PREORDER, "PREORDER": PREORDER, "COMING_SOON": COMING_SOON, "NOT_AVAILABLE": OUT_OF_STOCK}


def parse_search_page(page: str) -> list[Listing]:
    raw = extract_json_script(page, "__NEXT_DATA__")
    if not raw:
        return []
    data = json.loads(raw)
    out, seen = [], set()
    for d in walk(data):
        item_id = d.get("usItemId")
        name = d.get("name")
        if not item_id or not isinstance(name, str) or item_id in seen or ("priceInfo" not in d and "price" not in d):
            continue
        seen.add(item_id)
        avail = dig(d, "availabilityStatusV2", "value") or d.get("availabilityStatus") or d.get("availabilityStatusDisplayValue")
        status = STATUS.get(str(avail or "").upper().replace(" ", "_"), UNKNOWN)
        if d.get("isPreorder") or dig(d, "preOrder", "isPreOrder"):
            status = PREORDER if status in (IN_STOCK, UNKNOWN) else status
        seller = d.get("sellerName") or ""
        price = to_price(dig(d, "priceInfo", "currentPrice", "price")) or to_price(d.get("price"))
        url = d.get("canonicalUrl") or f"/ip/{item_id}"
        out.append(Listing(
            retailer="walmart", sku=str(item_id), title=clean(name),
            url=url if url.startswith("http") else f"https://www.walmart.com{url}",
            status=status, price=price, seller=seller,
            third_party=bool(seller) and seller.lower() not in ("walmart.com", "walmart"),
            image=d.get("image") or dig(d, "imageInfo", "thumbnailUrl", default=""),
        ))
    return out


class WalmartSource(Source):
    name = "walmart"
    label = "Walmart"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.offset = 0
        self.cycles = 0

    def queues(self) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
        """(every cycle, rotating). Page 1 of the broad searches catches restocks of anything popular,
        so it runs every cycle; deeper pages, newest-first and set-name searches take turns."""
        broad = [t for t, n in self.ctx.search_plan if n > 1]
        always = [(t, 1, "") for t in broad[:2]]
        rotating = [(t, p, "") for t, n in self.ctx.search_plan if n > 1 for p in range(1, n + 1)
                    if (t, p, "") not in always]
        rotating += [(t, 1, "new") for t in broad[:2]]
        rotating += [(t, 1, "") for t, n in self.ctx.search_plan if n <= 1]
        return always, rotating

    def plan(self) -> list[tuple[str, int, str]]:
        """At most `max_requests_per_cycle` searches. Walmart's bot protection blocks bursts,
        and a block costs far more coverage than a smaller, steady search plan."""
        always, rotating = self.queues()
        budget = max(1, int(self.scfg.get("max_requests_per_cycle", 6)) - len(always))
        if rotating:
            self.offset %= len(rotating)
            rotating = (rotating[self.offset:] + rotating[:self.offset])[:budget]
            self.offset += budget
        return always + rotating

    def check(self) -> CheckResult:
        res = CheckResult()
        seen: set[str] = set()
        self.cycles += 1
        always, rotating = self.queues()
        budget = max(1, int(self.scfg.get("max_requests_per_cycle", 6)) - len(always))
        # until every search has run once, anything found is "already existed", not news
        warming_up = self.cycles <= max(1, -(-len(rotating) // budget))
        for i, (term, pg, sort) in enumerate(self.plan()):
            if i:
                time.sleep(random.uniform(4, 9))  # human-ish pacing
            try:
                params = {"q": term} if pg == 1 else {"q": term, "page": pg}
                if sort:
                    params["sort"] = sort
                page = self.http.get("https://www.walmart.com/search", params=params).text
            except Exception as e:  # noqa: BLE001
                log.info("walmart '%s': %s", term, e)
                res.complete = False
                break  # if Walmart is blocking, don't hammer it with the other terms
            for l in parse_search_page(page):
                if l.sku not in seen:
                    seen.add(l.sku)
                    l.new_discovery = not warming_up
                    res.listings.append(l)
        return res
