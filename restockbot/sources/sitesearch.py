"""Watch other retailers' website search results (Costco, Dollar General, Five Below,
Ace Hardware, Dollar Tree/Family Dollar...). Each site is configured by a search URL, and
products are read from whatever the page offers, in this order:

1. schema.org JSON-LD (Product / ItemList), which many stores publish for Google
2. embedded app JSON (__NEXT_DATA__, application/json script blocks)
3. plain product links whose text mentions Pokémon / One Piece (stock unknown)

With only (3), the bot can still alert you when a new card product shows up on the site.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from urllib.parse import quote, quote_plus, urljoin

from ..models import COMING_SOON, IN_STOCK, LIMITED, OUT_OF_STOCK, PREORDER, UNKNOWN, CheckResult, Listing
from .base import Source, clean, dig, to_price, walk

log = logging.getLogger(__name__)

# Starting presets. If a site changes its search address, fix it in config.yaml.
PRESETS = {
    "costco": {"name": "Costco", "search_url": "https://www.costco.com/CatalogSearch?dept=All&keyword={q}"},
    "samsclub": {"name": "Sam's Club", "search_url": "https://www.samsclub.com/s/{q}"},
    "gamestop": {"name": "GameStop", "search_url": "https://www.gamestop.com/search/?q={q}"},
    "barnesandnoble": {"name": "Barnes & Noble", "search_url": "https://www.barnesandnoble.com/s/{q}"},
    "dollargeneral": {"name": "Dollar General", "search_url": "https://www.dollargeneral.com/search?q={q}"},
    "fivebelow": {"name": "Five Below", "search_url": "https://www.fivebelow.com/search?q={q}"},
    "acehardware": {"name": "Ace Hardware", "search_url": "https://www.acehardware.com/search?query={q}"},
    "dollartree": {"name": "Dollar Tree", "search_url": "https://www.dollartree.com/searchresults?Ntt={q}"},
    "familydollar": {"name": "Family Dollar", "search_url": "https://www.familydollar.com/search?q={q}"},
}

AVAIL = [
    (re.compile(r"preorder|pre_order|pre-order", re.I), PREORDER),
    (re.compile(r"outofstock|out_of_stock|out of stock|soldout|sold out|unavailable|discontinued", re.I), OUT_OF_STOCK),
    (re.compile(r"limited|lowstock|low_stock", re.I), LIMITED),
    (re.compile(r"instock|in_stock|in stock|available|onlineonly|instoreonly", re.I), IN_STOCK),
    (re.compile(r"comingsoon|coming soon|presale", re.I), COMING_SOON),
]


def map_avail(v) -> str:
    if isinstance(v, bool):
        return IN_STOCK if v else OUT_OF_STOCK
    s = str(v or "")
    for rx, status in AVAIL:
        if rx.search(s):
            return status
    return UNKNOWN


def _sku(url: str, fallback: str) -> str:
    m = re.search(r"(\d{5,})", url.split("?")[0])
    return m.group(1) if m else hashlib.md5((url or fallback).encode()).hexdigest()[:12]


def _availability(d: dict) -> str:
    offers = d.get("offers")
    for o in ([offers] if isinstance(offers, dict) else offers or []):
        if isinstance(o, dict) and o.get("availability"):
            return map_avail(o["availability"])
    for sub in walk(d):
        if sub is not d and isinstance(sub.get("name") or sub.get("productName"), str):
            continue  # a different (nested) product
        for k, v in sub.items():
            kl = k.lower()
            if ("avail" in kl or "stock" in kl or kl in ("purchasable", "orderable", "buyable")) \
                    and isinstance(v, (str, bool)):
                st = map_avail(v)
                if st != UNKNOWN:
                    return st
            if isinstance(v, dict) and ("avail" in kl or "stock" in kl):  # e.g. availabilityStatusV2: {value: ...}
                for vv in v.values():
                    if isinstance(vv, (str, bool)) and map_avail(vv) != UNKNOWN:
                        return map_avail(vv)
    return UNKNOWN


def _price(d: dict):
    offers = d.get("offers")
    for o in ([offers] if isinstance(offers, dict) else offers or []):
        if isinstance(o, dict):
            p = to_price(o.get("price") or o.get("lowPrice"))
            if p:
                return p
    p = to_price(dig(d, "priceInfo", "currentPrice", "price"))  # Walmart / Sam's Club format
    if p:
        return p
    for k in ("price", "salePrice", "currentPrice", "finalPrice", "listPrice", "regularPrice"):
        v = d.get(k)
        p = to_price(dig(v, "value") if isinstance(v, dict) else v) if v is not None else None
        if p:
            return p
    return None


def parse_page(page: str, base_url: str, retailer: str, shop_name: str) -> list[Listing]:
    found: dict[str, Listing] = {}

    def add(title, url, status, price):
        title = clean(title)
        if not title or len(title) < 6:
            return
        url = urljoin(base_url, url or "")
        sku = _sku(url, title)
        if sku not in found or (found[sku].status == UNKNOWN and status != UNKNOWN):
            found[sku] = Listing(retailer=retailer, sku=sku, title=title, url=url or base_url, status=status,
                                 price=price, seller=shop_name)

    blobs = []
    for m in re.finditer(r'<script[^>]+type="application/(?:ld\+)?json"[^>]*>(.*?)</script>', page, re.S | re.I):
        blobs.append(m.group(1))
    for m in re.finditer(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S):
        blobs.append(m.group(1))
    for raw in blobs:
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        for d in walk(data):
            t = d.get("@type")
            name = d.get("name") or d.get("title") or d.get("productName") or d.get("displayName")
            if not isinstance(name, str):
                continue
            url = d.get("url") or d.get("pdpUrl") or d.get("productUrl") or d.get("canonicalUrl") or d.get("link") or ""
            if not isinstance(url, str):
                url = ""
            is_product = t == "Product" or (isinstance(t, list) and "Product" in t) or (
                _price(d) is not None and (url or d.get("sku") or d.get("id")))
            if is_product:
                add(name, url, _availability(d), _price(d))

    # last resort: product links in the HTML
    if not found:
        for m in re.finditer(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
            text = clean(m.group(2))
            if re.search(r"pok[eé]mon|one piece", text, re.I) and len(text) < 200:
                add(text, m.group(1), UNKNOWN, None)
    return list(found.values())


def describe_page(page: str) -> str:
    """One-line description of a page we couldn't read, so the parser can be fixed."""
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    scripts = re.findall(r'(?:id|type)="([^"]+)"', " ".join(re.findall(r'<script[^>]*>', page)))
    kinds = sorted({x for x in scripts if "json" in x.lower() or x.startswith("__")})[:5]
    return (f"{len(page) // 1024}KB, title '{clean(title.group(1))[:50] if title else ''}', "
            f"'pokemon' x{len(re.findall('pok[eé]mon', page, re.I))}, data blocks: {', '.join(kinds) or 'none'}")


class SiteSearchSource(Source):
    name = "sitesearch"
    label = "Other stores"

    def sites(self) -> list[dict]:
        out = []
        for s in self.scfg.get("sites", []) or []:
            if isinstance(s, str):
                s = {"preset": s}
            preset = PRESETS.get(s.get("preset", ""), {})
            site = {**preset, **s}
            site.setdefault("id", s.get("preset") or re.sub(r"\W+", "", site.get("name", "site")).lower())
            if site.get("search_url") and s.get("enabled", True):
                out.append(site)
        return out

    def terms(self) -> list[str]:
        return self.scfg.get("terms") or ([] + (["pokemon cards"] if "pokemon" in self.ctx.games else [])
                                          + (["one piece card game"] if "onepiece" in self.ctx.games else []))

    def check(self) -> CheckResult:
        res = CheckResult()
        self.stats: list[str] = []
        self.diag: dict[str, str] = {}
        primed = getattr(self, "primed", set())
        self.primed = primed
        for site in self.sites():
            n = 0
            for term in self.terms():
                before_q = site["search_url"].split("{q}")[0]
                # the term goes in the path (samsclub.com/s/pokemon%20cards) or in a ?query
                q = quote_plus(term) if "?" in before_q else quote(term)
                url = site["search_url"].replace("{q}", q)
                try:
                    page = self.http.get(url).text
                except Exception as e:  # noqa: BLE001
                    log.info("%s: %s", site["name"], e)
                    self.stats.append(f"{site['name']}: FAILED {str(e)[:80]}")
                    res.complete = False
                    break
                got = parse_page(page, url, site["id"], site["name"])
                if not got and term == self.terms()[0]:
                    self.diag[site["id"]] = describe_page(page)
                for l in got:
                    # big-box bundles (e.g. Costco 3-ETB packs) don't fit single-item MSRPs, and these
                    # stores don't have marketplace scalpers, so skip the price filter
                    l.trusted_price = True
                    l.new_discovery = site["id"] in primed  # first successful read of a site = baseline
                    res.listings.append(l)
                    n += 1
                time.sleep(4)
            else:
                extra = f" [{self.diag[site['id']]}]" if not n and site["id"] in self.diag else ""
                self.stats.append(f"{site['name']}: {n} products read{extra}")
                primed.add(site["id"])
        return res

    def doctor(self) -> str:
        res = self.check()
        matched = sum(1 for l in res.listings if self.ctx.matcher.match(l.title))
        return (f"{matched} card products of {len(res.listings)} read" + "".join(
            f"\n      {line}" for line in getattr(self, "stats", [])))
