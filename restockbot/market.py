"""Resale-price gate: a store listing is only alert-worthy when its price is below what the same
product costs on TCGplayer (market price and lowest current listing, both without shipping).

Retail titles are matched to TCGplayer products by search, then checked for the same set words
and the same product type (an ETB never matches a booster bundle). Matches are cached; prices
refresh every few hours.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from .matcher import Match, norm
from .sources.base import to_price, walk

log = logging.getLogger(__name__)

SEARCH = "https://mp-search-api.tcgplayer.com/v1/search/request"
DETAILS = "https://mp-search-api.tcgplayer.com/v1/product/{pid}/details"
PRODUCT_LINE = {"pokemon": "pokemon", "onepiece": "one-piece-card-game"}

# words that say nothing about which product it is
FILLER = set(norm("""pokemon pokémon tcg trading card game cards card the a an of and & with
    mega evolution scarlet violet sword shield one piece english version new sealed box pack""").split())
TYPE_WORDS = ["pokemon center", "elite trainer", "booster bundle", "booster box", "booster display", "ultra premium",
              "super premium", "premium collection", "binder", "build and battle", "battle deck", "mini tin", "tin",
              "blister", "booster pack", "sleeved", "starter deck", "double pack", "figure collection", "ex box",
              "poster collection", "sticker", "gift collection", "illustration", "3 pack", "2 pack"]


@dataclass
class Market:
    product_id: int
    name: str
    market: Optional[float]
    lowest: Optional[float]   # lowest current listing, shipping excluded

    @property
    def ceiling(self) -> Optional[float]:
        """The price a deal has to beat: the lower of market and lowest listing."""
        vals = [v for v in (self.market, self.lowest) if v]
        return min(vals) if vals else None


def type_of(n: str) -> set[str]:
    return {t for t in TYPE_WORDS if f" {t} " in n}


def key_words(n: str) -> set[str]:
    return {w for w in n.split() if w not in FILLER and len(w) > 1}


def same_product(retail_title: str, tcg_name: str) -> float:
    """0..1 similarity; 0 when product types differ."""
    a, b = norm(retail_title), norm(tcg_name)
    ta, tb = type_of(a), type_of(b)
    if ta != tb and not (ta <= tb or tb <= ta):
        return 0.0
    if ("pokemon center" in ta) != ("pokemon center" in tb):
        return 0.0
    ka, kb = key_words(a), key_words(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)


def parse_search(data: dict) -> list[dict]:
    out, seen = [], set()
    for d in walk(data):
        pid = d.get("productId")
        name = d.get("productName")
        if pid and isinstance(name, str) and pid not in seen:
            seen.add(pid)
            out.append({"id": int(pid), "name": name, "market": to_price(d.get("marketPrice")),
                        "lowest": to_price(d.get("lowestPrice"))})
    return out


def parse_details(data: dict) -> tuple[Optional[float], Optional[float]]:
    d = data.get("results", [data])[0] if isinstance(data.get("results"), list) else data
    return to_price(d.get("marketPrice")), to_price(d.get("lowestPrice"))  # lowestPrice excludes shipping


class MarketLookup:
    def __init__(self, http, db, refresh_hours: float = 6, min_similarity: float = 0.5):
        self.http = http
        self.db = db
        self.refresh = refresh_hours * 3600
        self.min_sim = min_similarity
        self.failures = 0          # consecutive lookup errors (site down / changed), not "no match"

    def _search(self, title: str, game: str) -> list[dict]:
        q = " ".join(w for w in norm(title).split() if w not in ("trading", "card", "game", "tcg"))[:120]
        body = {"algorithm": "sales_synonym_v2", "from": 0, "size": 12,
                "filters": {"term": {"productLineName": [PRODUCT_LINE.get(game, game)],
                                     "productTypeName": ["Sealed Products"]}, "range": {}, "match": {}},
                "listingSearch": {"context": {"cart": {}}, "filters": {"term": {}, "range": {}, "exclude": {}}},
                "context": {"cart": {}, "shippingCountry": "US"}, "settings": {"useFuzzySearch": True},
                "sort": {}}
        r = self.http.post(SEARCH, params={"q": q, "isList": "false"}, data=json.dumps(body),
                           headers={"Content-Type": "application/json"})
        if r.status_code >= 400:
            raise RuntimeError(f"TCGplayer search {r.status_code}")
        return parse_search(r.json())

    def find(self, title: str, game: str, known_id: Optional[int] = None) -> Optional[Market]:
        k = f"tcgmatch:{norm(title).strip()}"
        cached = self.db.get(k)
        now = time.time()
        if cached and now - cached.get("ts", 0) < self.refresh:
            return Market(**cached["m"]) if cached.get("m") else None
        try:
            if known_id or (cached and cached.get("m")):
                pid = known_id or cached["m"]["product_id"]
                market, lowest = parse_details(self.http.json(DETAILS.format(pid=pid), check_block=False))
                name = cached["m"]["name"] if cached and cached.get("m") else str(pid)
                m = Market(pid, name, market, lowest)
            else:
                cands = self._search(title, game)
                scored = sorted(((same_product(title, c["name"]), c) for c in cands), key=lambda t: -t[0])
                if not scored or scored[0][0] < self.min_sim:
                    log.info("no TCGplayer match for '%s' (best: %s)", title[:60],
                             f"{scored[0][1]['name'][:50]} {scored[0][0]:.2f}" if scored else "none")
                    m = None
                else:
                    c = scored[0][1]
                    m = Market(c["id"], c["name"], c["market"], c["lowest"])
        except Exception as e:  # noqa: BLE001
            log.info("TCGplayer lookup failed for '%s': %s", title[:60], e)
            self.failures += 1
            return Market(**cached["m"]) if cached and cached.get("m") else None  # stale is better than none
        self.failures = 0
        self.db.set(k, {"ts": now, "m": m.__dict__ if m else None})
        if m:
            self.db.x("INSERT OR REPLACE INTO prices (product, ts, market, low) VALUES (?,?,?,?)",
                      (f"tcg:{m.product_id}", now, m.market, m.lowest))
        return m


class PriceGate:
    """Decides whether a listing's price is a deal vs TCGplayer."""

    def __init__(self, cfg: dict, lookup: Optional[MarketLookup]):
        g = cfg["alerts"].get("deal_filter", {}) or {}
        self.enabled = g.get("enabled", True) and lookup is not None
        self.compare = g.get("compare", "both")          # both | market | lowest
        self.unknown = g.get("when_no_tcgplayer_price", "skip")  # skip | alert
        self.require_retail = bool(g.get("retail_only", False))
        self.lookup = lookup

    def check(self, title: str, price: Optional[float], m: Match) -> tuple[bool, str]:
        """(alert?, line for the message)"""
        if not self.enabled:
            return True, ""
        if price is None:
            return self.unknown == "alert", "Store price not shown, so it can't be compared with TCGplayer"
        mk = self.lookup.find(title, m.game, m.tcgplayer_id)
        if not mk and getattr(self.lookup, "failures", 0) >= 3:
            # TCGplayer itself is unreachable; going silent would hide every restock, so fall back to MSRP
            ok = m.msrp is None or price <= m.msrp * 1.15
            return ok, "⚠️ TCGplayer unavailable right now, so this was checked against MSRP instead"
        if not mk or not mk.ceiling:
            return self.unknown == "alert", "⚠️ No TCGplayer price found to compare"
        bar = {"market": mk.market, "lowest": mk.lowest}.get(self.compare) or mk.ceiling
        saving = bar - price
        parts = []
        if mk.market:
            parts.append(f"market ${mk.market:,.2f}")
        if mk.lowest:
            parts.append(f"lowest listing ${mk.lowest:,.2f}")
        line = f"TCGplayer (no shipping): {', '.join(parts)}"
        if saving > 0:
            return True, f"💰 ${saving:,.2f} below TCGplayer ({line})"
        return False, line
