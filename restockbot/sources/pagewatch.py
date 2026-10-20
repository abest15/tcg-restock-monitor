"""Watch any product page (GameStop, Costco, Sam's Club, Barnes & Noble, a local shop...)
by looking for in-stock / out-of-stock text on the page."""
from __future__ import annotations

import logging
import re
import time

from ..models import IN_STOCK, OUT_OF_STOCK, UNKNOWN, CheckResult, Listing
from .base import Source
from .pokemoncenter import page_price, page_title

log = logging.getLogger(__name__)

DEFAULT_IN = r"add to cart|add to bag|in stock|pick up today|ship it"
DEFAULT_OUT = r"sold out|out of stock|currently unavailable"


def classify(page: str, in_re: str, out_re: str) -> str:
    t = re.sub(r"\s+", " ", page.lower())
    if re.search(out_re, t):
        return OUT_OF_STOCK
    if re.search(in_re, t):
        return IN_STOCK
    return UNKNOWN


class PageWatchSource(Source):
    name = "pagewatch"
    label = "Watched page"

    def check(self) -> CheckResult:
        res = CheckResult()
        for p in self.scfg.get("pages", []) or []:
            url = p["url"]
            try:
                page = self.http.get(url).text
            except Exception as e:  # noqa: BLE001
                log.info("pagewatch %s: %s", url, e)
                res.complete = False
                continue
            status = classify(page, p.get("in_stock_regex", DEFAULT_IN).lower(), p.get("out_of_stock_regex", DEFAULT_OUT).lower())
            host = url.split("/")[2].replace("www.", "")
            res.listings.append(Listing(
                retailer=p.get("retailer", host.split(".")[0]), sku=re.sub(r"\W+", "-", url)[-80:],
                title=p.get("name") or page_title(page, url), url=url, status=status,
                price=p.get("price") or page_price(page)))
            time.sleep(2)
        return res
