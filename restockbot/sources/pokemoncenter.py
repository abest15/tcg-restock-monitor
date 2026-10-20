"""Pokémon Center: watches for the virtual queue (it goes up right before big drops) and
for stock on specific product pages you list in config.yaml."""
from __future__ import annotations

import logging
import re
import time

from ..models import COMING_SOON, IN_STOCK, OUT_OF_STOCK, UNKNOWN, CheckResult, Listing, Signal
from ..http import Blocked
from .base import Source, clean

log = logging.getLogger(__name__)

HOME = "https://www.pokemoncenter.com/"
QUEUE_MARKERS = ("queue-it", "you are now in line", "you are in line", "virtual queue", "waiting room",
                 "your estimated wait time", "you're in the queue", "high traffic", "queue.pokemoncenter")


def detect_queue(final_url: str, page: str) -> bool:
    text = (final_url + " " + page[:50000]).lower()
    return any(m in text for m in QUEUE_MARKERS)


def page_status(page: str) -> str:
    t = re.sub(r"\s+", " ", page.lower())
    if re.search(r"(sold out|out of stock|currently unavailable|no longer available)", t):
        return OUT_OF_STOCK
    if re.search(r"(coming soon|notify me when available|available soon)", t):
        return COMING_SOON
    if re.search(r"(add to cart|add to bag|addtocart)", t):
        return IN_STOCK
    return UNKNOWN


def page_title(page: str, fallback: str) -> str:
    m = re.search(r'<meta property="og:title" content="([^"]+)"', page) or re.search(r"<title>(.*?)</title>", page, re.S)
    return clean(m.group(1)).replace(" | Pokémon Center Official Site", "") if m else fallback


def page_price(page: str):
    m = re.search(r'"price"\s*:\s*"?(\d+\.\d{2})', page) or re.search(r'\$(\d+\.\d{2})', page)
    return float(m.group(1)) if m else None


class PokemonCenterSource(Source):
    name = "pokemoncenter"
    label = "Pokémon Center"

    def check(self) -> CheckResult:
        res = CheckResult()
        try:
            r = self.http.get(HOME, check_block=False)
            if detect_queue(r.url, r.text):
                res.signals.append(Signal(
                    source=self.name, key=f"pc-queue:{time.strftime('%Y-%m-%d-%H')}",
                    title="Pokémon Center queue is UP",
                    message="The Pokémon Center virtual queue is active. That usually means a drop is live or about to start. "
                            "Join the queue now (keep the tab open; don't refresh).",
                    url=HOME, priority=5, tags=["rotating_light", "pokeball"], game="pokemon"))
        except Blocked:
            # Imperva blocks scripted traffic often; a block during drops can itself mean heavy traffic.
            res.complete = False
        except Exception as e:  # noqa: BLE001
            log.info("pokemon center home: %s", e)
            res.complete = False
        for entry in self.scfg.get("product_urls", []) or []:
            url = entry["url"] if isinstance(entry, dict) else entry
            try:
                r = self.http.get(url)
            except Exception as e:  # noqa: BLE001
                log.info("pokemon center %s: %s", url, e)
                res.complete = False
                continue
            sku = url.rstrip("/").split("/")[-1].split("?")[0]
            name = entry.get("name", sku) if isinstance(entry, dict) else sku
            res.listings.append(Listing(retailer="pokemoncenter", sku=sku, title=page_title(r.text, name),
                                        url=url, status=page_status(r.text), price=page_price(r.text),
                                        seller="Pokémon Center"))
            time.sleep(2)
        return res
