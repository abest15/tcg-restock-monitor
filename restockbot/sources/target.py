"""Target: online stock plus per-store stock for every Target within your radius.

Uses the same RedSky JSON endpoints target.com's own pages call. Store-level
`location_available_to_promise_quantity` often goes up when a delivery is
received, which can be before the product is on the shelf.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Optional

from ..models import (COMING_SOON, IN_STOCK, LIMITED, OUT_OF_STOCK, PREORDER, UNKNOWN, CheckResult,
                      Listing, StoreStock)
from ..config import ROOT
from ..http import Blocked
from .base import Source, clean, dig, to_price, walk

log = logging.getLogger(__name__)

BASE = "https://redsky.target.com/redsky_aggregations/v1/web"
BASE_PLATFORM = "https://redsky.target.com/redsky_aggregations/v1/web_platform"
LOCATIONS = "https://api.target.com/location_proximities/v1/nearby_locations"
FALLBACK_KEYS = ["9f36aeafbe60771e321a7cc95a78140772ab3e96", "ff457966e64d5e877fdbad070f276d18ecec4a01"]

STATUS = {
    "IN_STOCK": IN_STOCK,
    "LIMITED_STOCK": LIMITED,
    "PRE_ORDER_SELLABLE": PREORDER,
    "PRE_ORDER_UNSELLABLE": COMING_SOON,
    "OUT_OF_STOCK": OUT_OF_STOCK,
    "UNAVAILABLE": OUT_OF_STOCK,
    "NOT_SOLD_IN_STORE": OUT_OF_STOCK,
    "DISCONTINUED": OUT_OF_STOCK,
}


def map_status(s: Optional[str]) -> str:
    return STATUS.get(str(s or "").upper(), UNKNOWN)


def best_status(*statuses: str) -> str:
    order = [IN_STOCK, LIMITED, PREORDER, COMING_SOON, OUT_OF_STOCK, UNKNOWN]
    present = [s for s in statuses if s]
    return min(present, key=order.index) if present else UNKNOWN


def parse_search(data: dict) -> list[dict]:
    out = []
    products = dig(data, "data", "search", "products", default=[]) or []
    for p in products:
        tcin = str(p.get("tcin") or dig(p, "item", "tcin") or "")
        if not tcin:
            continue
        title = clean(dig(p, "item", "product_description", "title", default=""))
        url = dig(p, "item", "enrichment", "buy_url") or f"https://www.target.com/p/-/A-{tcin}"
        price = to_price(dig(p, "price", "current_retail")) or to_price(dig(p, "price", "formatted_current_price"))
        marketplace = any(d.get("is_marketplace") is True for d in walk(p.get("item", {})))
        out.append({
            "tcin": tcin, "title": title, "url": url, "price": price, "marketplace": marketplace,
            "image": dig(p, "item", "enrichment", "images", "primary_image_url", default=""),
            "release_date": dig(p, "item", "product_description", "street_date", default="")
                            or dig(p, "item", "street_date", default=""),
        })
    return out


def parse_nearby_stores(data: dict) -> list[dict]:
    stores, seen = [], set()
    for d in walk(data):
        sid = d.get("store_id") or d.get("location_id")
        name = d.get("location_name") or d.get("store_name") or dig(d, "location_names", 0, "name")
        if not sid or not name or str(sid) in seen:
            continue
        seen.add(str(sid))
        addr = d.get("mailing_address") or d.get("address") or {}
        if not isinstance(addr, dict):
            addr = {}
        address = ", ".join(x for x in [addr.get("address_line1"), addr.get("city"), addr.get("region") or addr.get("state")] if x)
        stores.append({"id": str(sid), "name": f"Target {name}", "distance": to_price(d.get("distance")),
                       "address": address, "state": addr.get("region") or addr.get("state") or "",
                       "zip": addr.get("postal_code", "")})
    stores.sort(key=lambda s: s["distance"] if s["distance"] is not None else 999)
    return stores


def _store_status(d: dict) -> str:
    qty = d.get("location_available_to_promise_quantity")
    statuses = [map_status(dig(d, k, "availability_status")) for k in ("in_store_only", "order_pickup", "curbside", "ship_to_store")]
    st = best_status(*statuses)
    if st == UNKNOWN and qty is not None:
        st = IN_STOCK if qty > 0 else OUT_OF_STOCK
    if qty is not None and qty <= 0 and st in (IN_STOCK, LIMITED):
        st = OUT_OF_STOCK
    return st


def parse_store_stock(data: dict, known: dict[str, dict]) -> list[StoreStock]:
    """Parses fiats_v1 or product_fulfillment_v1 store entries into StoreStock rows."""
    out, seen = [], set()
    for d in walk(data):
        sid = d.get("location_id") or d.get("store_id") or dig(d, "store", "location_id") or dig(d, "store", "store_id")
        if not sid or not any(k in d for k in ("location_available_to_promise_quantity", "in_store_only", "order_pickup")):
            continue
        sid = str(sid)
        if sid in seen:
            continue
        seen.add(sid)
        info = known.get(sid, {})
        qty = d.get("location_available_to_promise_quantity")
        name = d.get("location_name") or dig(d, "store", "location_name")
        out.append(StoreStock(
            store_id=sid,
            name=info.get("name") or (f"Target {name}" if name else f"Target #{sid}"),
            status=_store_status(d),
            quantity=int(qty) if isinstance(qty, (int, float)) else None,
            distance_miles=info.get("distance") if info.get("distance") is not None else to_price(d.get("distance")),
            address=info.get("address", ""),
            pickup=map_status(dig(d, "order_pickup", "availability_status")) in (IN_STOCK, LIMITED),
        ))
    return out


def parse_online_status(data: dict) -> str:
    ship = dig(data, "data", "product", "fulfillment", "shipping_options", "availability_status")
    if ship:
        return map_status(ship)
    for d in walk(data):
        if "shipping_options" in d:
            return map_status(dig(d, "shipping_options", "availability_status"))
    return UNKNOWN


class TargetSource(Source):
    name = "target"
    label = "Target"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.keys = [k for k in [self.scfg.get("api_key")] if k]
        self.visitor = self.ctx.db.get("target_visitor") or uuid.uuid4().hex.upper()
        self.ctx.db.set("target_visitor", self.visitor)
        self.stores: list[dict] = []
        self.stores_at = 0.0
        self.catalog: dict[str, dict] = self.ctx.db.get("target_catalog", {}) or {}
        for tcin in self.scfg.get("tcins", []) or []:  # manual fallback list from config.yaml
            self.catalog.setdefault(str(tcin), {"tcin": str(tcin), "title": "", "url": f"https://www.target.com/p/-/A-{tcin}",
                                                "priority": 4, "new": False})
        self.search_at = 0.0
        self.initial_done = bool(self.catalog)
        self.offset = 0
        self.geo: dict = self.ctx.db.get(f"geo:{self.ctx.zip}") or {}
        self.last_errors: dict[str, str] = {}

    # --- setup ---
    def api_keys(self) -> list[str]:
        if not self.keys:
            found = []
            try:
                page = self.http.get("https://www.target.com/", check_block=False).text
                found = re.findall(r'apiKey\\?"\s*:\s*\\?"([0-9a-f]{40})', page)
            except Exception as e:  # noqa: BLE001
                log.debug("target key discovery failed: %s", e)
            self.keys = list(dict.fromkeys(found + FALLBACK_KEYS))
        return self.keys

    def location(self) -> dict:
        """Latitude/longitude/state for the ZIP (Target's fulfillment API wants them)."""
        if not self.geo and self.ctx.zip:
            try:
                d = self.http.json(f"https://api.zippopotam.us/us/{self.ctx.zip}", check_block=False)
                place = d["places"][0]
                self.geo = {"lat": place["latitude"], "lon": place["longitude"], "state": place["state abbreviation"]}
                self.ctx.db.set(f"geo:{self.ctx.zip}", self.geo)
            except Exception as e:  # noqa: BLE001
                log.info("zip lookup failed: %s", e)
        return self.geo

    def _try(self, label: str, variants: list[tuple[str, dict]]) -> dict:
        """Try each (url, params) variant with each API key until one answers."""
        err: Exception | None = None
        for key in self.api_keys():
            for url, params in variants:
                try:
                    data = self.http.json(url, params={"key": key, **params}, check_block=False)
                    if self.keys[0] != key:  # remember the key that works
                        self.keys.remove(key)
                        self.keys.insert(0, key)
                    self.last_errors.pop(label, None)
                    return data
                except Exception as e:  # noqa: BLE001
                    err = e
                    if isinstance(e, Blocked) or any(c in str(e) for c in ("429", "435", "cooling down")):
                        self.last_errors[label] = str(e)[:300]
                        raise  # bot protection: retrying with another key only makes it worse
        self.last_errors[label] = str(err)[:300]
        raise err or RuntimeError(f"target {label} failed")

    def _web(self, endpoint: str, params: dict) -> list[tuple[str, dict]]:
        p = {"channel": "WEB", "visitor_id": self.visitor, **params}
        return [(f"{BASE}/{endpoint}", p)]

    def nearby_stores(self) -> list[dict]:
        if self.stores_at and time.time() - self.stores_at < 86400:
            return self.stores
        z, r = self.ctx.zip, int(min(self.ctx.radius, 100))
        base = {"limit": 20, "within": r, "place": z}
        try:
            data = self._try("stores", [
                (f"{BASE}/nearby_stores_v1", base),
                (f"{BASE}/nearby_stores_v1", {**base, "channel": "WEB"}),
                (f"{BASE_PLATFORM}/nearby_stores_v1", base),
                (LOCATIONS, {**base, "unit": "mile", "type": "store"}),
            ])
            stores = [s for s in parse_nearby_stores(data) if s["distance"] is None or s["distance"] <= self.ctx.radius]
            self.stores = stores[: int(self.scfg.get("max_stores", 20))]
        except Exception as e:  # noqa: BLE001
            # not fatal: per-product store checks (fiats) also return nearby stores
            log.warning("target store list unavailable (%s); using per-product store data", str(e)[:150])
            self.stores = []
            self.stores_at = time.time() - 86400 + 1800  # retry in 30 minutes, not tomorrow
            return self.stores
        self.stores_at = time.time()
        return self.stores

    # --- discovery ---
    def seed_tcins(self) -> dict[str, str]:
        """Target item numbers from target_items.txt (pasted links) and from Reddit deal posts."""
        seeds: dict[str, str] = dict(self.ctx.db.get("target_seed_tcins", {}) or {})
        f = ROOT / "target_items.txt"
        if f.exists():
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    m = re.search(r"A-(\d{6,10})", line) or re.fullmatch(r"(\d{6,10})", line)
                    if m:
                        seeds.setdefault(m.group(1), "")
        return seeds

    def add_seeds(self) -> None:
        for tcin, hint in self.seed_tcins().items():
            if tcin in self.catalog:
                continue
            # title is filled in from Target on the first stock check; until then use the post title
            self.catalog[tcin] = {"tcin": tcin, "title": hint, "url": f"https://www.target.com/p/-/A-{tcin}",
                                  "priority": 4, "new": self.initial_done, "needs_title": True}

    def discover(self, max_searches: int | None = None) -> None:
        """Searches Target slowly: a few terms per run, rotating. Target's bot protection
        blocks bursts of searches, and a block also stops the stock checks for a while."""
        self.add_seeds()
        stores = self.nearby_stores()
        home = stores[0]["id"] if stores else str(self.scfg.get("home_store_id", "")) or "3991"
        broad = [t for t, n in self.ctx.search_plan if n > 1][:2]
        # newest-first passes catch brand-new product pages (pre-orders) that relevance sorting buries
        plan = [(t, p, "") for t, n in self.ctx.search_plan for p in range(n)] + [(t, 0, "newest") for t in broad]
        k = max_searches or int(self.scfg.get("searches_per_run", 4))
        self.search_stats: list[str] = []
        if not plan:
            return
        start = self.ctx.db.get("target_search_offset", 0) % len(plan)
        batch = (plan[start:] + plan[:start])[:k]
        for i, (term, page, sort) in enumerate(batch):
            params = {
                "keyword": term, "count": 24, "offset": page * 24, "default_purchasability_filter": "true",
                "include_sponsored": "true", "new_search": "true", "page": f"/s/{term}", "platform": "desktop",
                "pricing_store_id": home, "spellcheck": "true",
                "store_ids": ",".join(s["id"] for s in stores[:5]) or home, "zip": self.ctx.zip,
            }
            if sort:
                params["sort_by"] = sort
            try:
                data = self._try("search", self._web("plp_search_v2", params))
            except Exception as e:  # noqa: BLE001
                log.warning("target search '%s' failed: %s", term, str(e)[:120])
                self.search_stats.append(f"'{term}' p{page + 1}: ERROR {str(e)[:100]}")
                break  # blocked: stop searching this round, stock checks continue
            found = parse_search(data)
            n_match = 0
            for item in found:
                m = self.ctx.matcher.match(item["title"])
                if m:
                    n_match += 1
                    item["priority"] = m.priority
                    old = self.catalog.get(item["tcin"])
                    item["new"] = old.get("new", False) if old else self.initial_done
                    self.catalog[item["tcin"]] = {**(old or {}), **item, "needs_title": False}
            sample = "; ".join(i["title"][:35] for i in found[:2])
            self.search_stats.append(f"'{term}' p{page + 1}: {len(found)} results, {n_match} cards [{sample}]")
            self.ctx.db.set("target_search_offset", (start + i + 1) % len(plan))
            if (start + i + 1) >= len(plan):
                self.initial_done = True  # every term searched once; new finds from now on are news
            if i < len(batch) - 1:
                time.sleep(float(self.scfg.get("search_spacing_seconds", 20)))
        # drop catalog entries that no longer match (e.g. toys caught by an old rule)
        self.catalog = {k: v for k, v in self.catalog.items()
                        if v.get("needs_title") or not v.get("title") or self.ctx.matcher.match(v["title"])}
        self.ctx.db.set("target_catalog", self.catalog)
        self.search_at = time.time()

    # --- stock ---
    def product_stock(self, tcin: str) -> tuple[str, list[StoreStock], bool]:
        stores = self.nearby_stores()
        known = {s["id"]: s for s in stores}
        home = stores[0] if stores else {}
        geo = self.location()
        online = UNKNOWN
        store_rows: list[StoreStock] = []
        hid = home.get("id") or str(self.scfg.get("home_store_id", ""))
        pf_params = {"tcin": tcin, "is_bot": "false", "zip": self.ctx.zip, "state": home.get("state") or geo.get("state", ""),
                     "latitude": geo.get("lat", ""), "longitude": geo.get("lon", ""),
                     "paid_membership": "false", "base_membership": "false", "card_membership": "false",
                     "page": f"/p/A-{tcin}"}
        if hid:
            pf_params.update({"store_id": hid, "scheduled_delivery_store_id": hid, "required_store_id": hid,
                              "has_required_store_id": "true"})
        try:
            pf = self._try("fulfillment", self._web("product_fulfillment_v1", pf_params)
                           + self._web("pdp_client_v1", {"tcin": tcin, "pricing_store_id": hid or "3991",
                                                          "store_id": hid or "3991", "zip": self.ctx.zip,
                                                          "page": f"/p/A-{tcin}"}))
            online = parse_online_status(pf)
            store_rows = parse_store_stock(pf, known)
            entry = self.catalog.get(tcin, {})
            if entry.get("needs_title") or not entry.get("title"):
                title = clean(dig(pf, "data", "product", "item", "product_description", "title", default=""))
                if title:
                    entry.update({"title": title, "needs_title": False})
                    m = self.ctx.matcher.match(title)
                    entry["priority"] = m.priority if m else 0
                    self.catalog[tcin] = entry
        except Exception as e:  # noqa: BLE001
            log.debug("target fulfillment %s failed: %s", tcin, e)
        checked = False
        try:
            fi = self._try("store_stock", self._web("fiats_v1", {
                "tcin": tcin, "nearby": self.ctx.zip, "radius": int(min(self.ctx.radius, 100)), "limit": 50,
                "include_only_available_stores": "false", "requested_quantity": 1,
            }) + [(f"{BASE}/fiats_v1", {"tcin": tcin, "nearby": self.ctx.zip, "radius": int(min(self.ctx.radius, 100)),
                                        "limit": 20, "include_only_available_stores": "false", "requested_quantity": 1})])
            rows = parse_store_stock(fi, known)
            if rows:
                by_id = {r.store_id: r for r in store_rows}
                by_id.update({r.store_id: r for r in rows})
                store_rows = list(by_id.values())
                checked = True
        except Exception as e:  # noqa: BLE001
            log.debug("target fiats %s failed: %s", tcin, e)
        store_rows = [r for r in store_rows if r.distance_miles is None or r.distance_miles <= self.ctx.radius]
        return online, store_rows, checked

    def check(self) -> CheckResult:
        if not self.ctx.zip:
            return CheckResult(complete=False)
        if time.time() - self.search_at > float(self.scfg.get("search_interval_seconds", 1800)):
            self.discover()
        else:
            self.add_seeds()
        res = CheckResult()
        if not self.catalog:
            res.complete = False
            return res
        # watchlist (high priority) items every cycle; the rest rotate through the remaining slots
        max_n = int(self.scfg.get("max_products_per_cycle", 40))
        # urgent (max) items every cycle; other watched items rotate through most of the slots,
        # generic catches rotate through what's left
        # priority 0 = turned out not to be a card product (e.g. a seed link to a toy)
        items = [i for i in self.catalog.values() if i.get("priority", 3) > 0]
        urgent = [i for i in items if i.get("priority", 3) >= 5]
        watched = [i for i in items if i.get("priority", 3) == 4]
        room = max(5, int((max_n - len(urgent)) * 0.75))
        if len(watched) > room:
            self.hoffset = getattr(self, "hoffset", 0) % len(watched)
            watched = (watched[self.hoffset:] + watched[:self.hoffset])[:room]
            self.hoffset += room
        high = urgent + watched
        low = [i for i in items if i.get("priority", 3) < 4]
        slots = max(5, max_n - len(high))
        if low:
            self.offset %= len(low)
            low_due = (low[self.offset:] + low[:self.offset])[:slots]
            self.offset += slots
        else:
            low_due = []
        for item in high + low_due:
            try:
                online, stores, checked = self.product_stock(item["tcin"])
            except Exception as e:  # noqa: BLE001
                log.warning("target %s: %s", item["tcin"], e)
                res.complete = False
                continue
            if online == UNKNOWN and not stores:
                res.complete = False
                continue
            res.listings.append(Listing(
                retailer="target", sku=item["tcin"], title=item.get("title") or self.catalog.get(item["tcin"], {}).get("title", ""),
                url=item["url"], status=online,
                price=item.get("price"), third_party=item.get("marketplace", False),
                seller="Target Plus partner" if item.get("marketplace") else "Target",
                image=item.get("image", ""), release_date=item.get("release_date", ""),
                stores=stores, stores_checked=checked, new_discovery=item.get("new", True),
            ))
            time.sleep(float(self.scfg.get("request_spacing_seconds", 1.5)))
        self.ctx.db.set("target_catalog", self.catalog)
        return res

    def doctor(self) -> str:
        stores = self.nearby_stores()
        self.discover(max_searches=2)
        lines = [f"stores: {len(stores)} Targets within {self.ctx.radius:g} mi"
                 + (" (" + ", ".join(f"{s['name']} {s['distance']} mi" for s in stores[:3]) + ")" if stores else "")]
        lines.append(f"tracking {len(self.catalog)} Target products ({len(self.seed_tcins())} from links/Reddit)")
        if self.catalog:
            tcin = max(self.catalog.values(), key=lambda i: i.get("priority", 3))["tcin"]
            online, rows, checked = self.product_stock(tcin)
            instock = sum(1 for r in rows if r.status in (IN_STOCK, LIMITED))
            lines.append(f"sample '{(self.catalog[tcin].get('title') or tcin)[:45]}': online={online}, "
                         f"{len(rows)} nearby stores checked, {instock} have it")
        for st in getattr(self, "search_stats", []):
            lines.append(f"\n      search {st}")
        for label, err in self.last_errors.items():
            lines.append(f"\n      {label} error: {err}")
        ok = self.catalog and not {"fulfillment", "store_stock"} & set(self.last_errors)
        return ("OK: " if ok else "PROBLEM: ") + " | ".join(lines)
