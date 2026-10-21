from __future__ import annotations

import logging
import random
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from . import reports
from .db import DB
from .matcher import Match, Matcher
from .models import BUYABLE, COMING_SOON, OUT_OF_STOCK, PREORDER, UNKNOWN, CheckResult, Listing, Signal, StoreStock, is_buyable
from .notify import Notifier, maps_url, view_action
from .sources.base import Source
from .sources.tcgplayer import latest_market

log = logging.getLogger(__name__)

RETAILER_NAMES = {"target": "Target", "bestbuy": "Best Buy", "walmart": "Walmart", "pokemoncenter": "Pokémon Center",
                  "costco": "Costco", "kroger": "King Soopers / Kroger", "gamestop": "GameStop",
                  "barnesandnoble": "Barnes & Noble", "samsclub": "Sam's Club", "dollargeneral": "Dollar General", "fivebelow": "Five Below",
                  "acehardware": "Ace Hardware", "dollartree": "Dollar Tree", "familydollar": "Family Dollar"}
GAME_TAG = {"pokemon": "pokeball", "onepiece": "pirate_flag"}


def retailer_name(r: str, l: Optional[Listing] = None) -> str:
    if r.startswith("shop:"):
        return (l.seller if l and l.seller else r[5:])
    return RETAILER_NAMES.get(r) or (l.seller if l and l.seller else r.title())


def cart_url(l: Listing) -> str:
    """Direct add-to-cart links, one tap faster than the product page during a drop."""
    if l.retailer == "walmart" and l.sku.isdigit():
        return f"https://affil.walmart.com/cart/addToCart?items={l.sku}"
    if l.retailer == "bestbuy" and l.sku.isdigit():
        return f"https://api.bestbuy.com/click/-/{l.sku}/cart"
    if l.retailer.startswith("shop:") and l.url:
        return l.url  # Shopify product page has the add button at the top
    return ""


def money(v: Optional[float]) -> str:
    return f"${v:,.2f}" if v is not None else "?"


class Engine:
    def __init__(self, cfg: dict, watchlist: dict, db: DB, matcher: Matcher, notifier: Notifier, sources: list[Source]):
        self.cfg = cfg
        self.watchlist = watchlist
        self.db = db
        self.matcher = matcher
        self.n = notifier
        self.sources = sources
        a = cfg["alerts"]
        self.cooldown = float(a["cooldown_minutes"]) * 60
        self.radius = float(cfg["location"]["radius_miles"])
        self.qty_jump = int(a["store_quantity_jump"])

    # ---------------- alert helpers ----------------
    def _send(self, dedupe: str, title: str, msg: str, *, priority: int, tags: list[str], url: str = "",
              actions: Optional[list[dict]] = None, cooldown: Optional[float] = None, image: str = "") -> None:
        if self.db.recently_sent(dedupe, self.cooldown if cooldown is None else cooldown):
            log.debug("dedup %s", dedupe)
            return
        if self.n.send(title, msg, priority=priority, tags=tags, click=url, actions=actions, image=image):
            self.db.mark_sent(dedupe)

    def _value_line(self, m: Match, price: Optional[float]) -> tuple[str, int]:
        """Returns a resale-value line and a priority bump for hot products."""
        if not m.watch_id:
            return "", 0
        market = latest_market(self.db, m.watch_id)
        if not market:
            return "", 0
        basis = price or m.msrp
        if not basis:
            return f"Market value: {money(market)}", 0
        ratio = market / basis
        if ratio >= 1.3:
            return f"Market value {money(market)} ({ratio:.1f}x what you'd pay)", 1 if ratio >= 1.6 else 0
        return f"Market value: {money(market)}", 0

    def _deal(self, l: Listing, m: Match, price_ok: bool) -> tuple[bool, str]:
        """Should a price alert go out? The price rules (retail + a small allowance, and not above
        market value) decide; cached per listing and price."""
        # only the store itself, never marketplace sellers (Walmart Marketplace, Target Plus, Best Buy
        # Marketplace), no matter the price
        if l.third_party and self.cfg["alerts"].get("first_party_only", True):
            log.info("no alert for %s at %s: third-party seller %s", l.title[:60], l.retailer, l.seller or "?")
            return False, "third-party seller"
        gate = getattr(self, "gate", None)
        if not gate or not gate.enabled:
            return price_ok, ""
        k = (l.key, l.price)
        cache = self.__dict__.setdefault("_deal_cache", {})
        if k not in cache:
            ok, line = gate.check(l.title, l.price, m, trusted=l.trusted_price)
            if not ok:
                log.info("no alert for %s at %s: %s (store %s)", l.title[:60], l.retailer, line, money(l.price))
            cache[k] = (ok, line)
            if len(cache) > 5000:
                cache.clear()
        return cache[k]

    @staticmethod
    def _retail_adj(l: Listing, m: Match) -> int:
        """Retail is strongly preferred: +1 priority at/below MSRP, -1 when above it."""
        if l.price is None or not m.msrp:
            return 0
        if l.trusted_price and l.price > m.msrp * 1.15:
            return 0  # warehouse-club bundles: several items, can't compare with one item's MSRP
        return 1 if l.price <= m.msrp * 1.02 else (-1 if l.price > m.msrp * 1.15 else 0)

    def _price_line(self, l: Listing, m: Match) -> str:
        parts = [f"Price {money(l.price)}"]
        if m.msrp:
            parts.append(f"MSRP {money(m.msrp)}")
        parts.append(f"sold by {l.seller}" if l.third_party and l.seller
                     else f"sold by {retailer_name(l.retailer, l)}")
        return " · ".join(parts)

    # ---------------- core processing ----------------
    def process(self, source: Source, res: CheckResult) -> None:
        baseline_key = f"baselined:{source.name}"
        baseline = not self.db.get(baseline_key, False)
        hot_now: list[str] = []
        for l in res.listings:
            m = self.matcher.match(l.title)
            if not m:
                continue
            tracker = getattr(self, "tracker", None)
            if tracker and l.retailer in ("target", "walmart", "bestbuy", "pokemoncenter"):
                new_set = tracker.learn_from_title(l.title, m.game)
                if new_set:
                    from .sources.sets import new_set_signal
                    self._signal(new_set_signal(new_set))
                    m = self.matcher.match(l.title) or m
            try:
                hot = self._process_listing(l, m, baseline)
                if hot:
                    hot_now.append(hot)
            except Exception:  # noqa: BLE001
                log.exception("processing %s", l.key)
        for s in res.signals:
            self._signal(s)
        if baseline and res.listings and res.complete:
            self.db.set(baseline_key, True)
            msg = (f"Tracking {len(res.listings)} listings. Alerts start with the next change.\n"
                   + ("Buyable right now:\n" + "\n".join(hot_now[:12]) if hot_now else "Nothing on your watchlist is buyable right now."))
            self.n.send(f"✅ {source.label} monitor is live", msg, priority=3, tags=["white_check_mark"])

    def _process_listing(self, l: Listing, m: Match, baseline: bool) -> Optional[str]:
        now = time.time()
        prev = self.db.listing(l.key)
        prev_status = prev["status"] if prev else None
        price_ok = l.trusted_price or self.matcher.price_ok(l.price, m)
        name = retailer_name(l.retailer, l)
        label = m.label if m.watch_name else l.title
        game_tag = GAME_TAG.get(m.game, "flower_playing_cards")

        changed = prev is None or prev_status != l.status
        # first sighting of something we already knew existed: record silently
        silent = baseline or (prev is None and not l.new_discovery)
        self.db.x("""INSERT INTO listings (key, retailer, sku, title, url, status, price, watch, game, first_seen, last_seen, last_change)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(key) DO UPDATE SET title=excluded.title, url=excluded.url, status=excluded.status,
                       price=COALESCE(excluded.price, listings.price), watch=excluded.watch, game=excluded.game,
                       last_seen=excluded.last_seen, last_change=CASE WHEN listings.status != excluded.status
                       THEN excluded.last_seen ELSE listings.last_change END""",
                  (l.key, l.retailer, l.sku, l.title, l.url, l.status, l.price, m.watch_id, m.game, now, now, now))

        value_line, bump = self._value_line(m, l.price)
        actions = [view_action("🛒 Buy now", l.url)]
        cart = cart_url(l)
        if cart:
            actions.insert(0, view_action("⚡ Add to cart", cart))
        hot_desc = None

        # --- online availability ---
        if is_buyable(l.status) and (prev is None or not is_buyable(prev_status)):
            deal_ok, deal_line = self._deal(l, m, price_ok) if not silent else (False, "")
            if deal_ok and not silent:
                verb = "PRE-ORDER OPEN" if l.status == PREORDER else ("NEW & IN STOCK" if prev is None else "RESTOCK")
                lines = [f"{label}" if m.watch_name else l.title, self._price_line(l, m)]
                if m.watch_name and l.title != label:
                    lines.append(l.title)
                if deal_line:
                    lines.append(deal_line)
                elif value_line:
                    lines.append(value_line)
                self._send(f"online:{l.key}", f"{'🚨' if m.priority >= 4 else '🟢'} {verb} at {name}: {label}"[:200],
                           "\n".join(lines), priority=max(2, min(5, m.priority + bump + self._retail_adj(l, m))),
                           url=l.url, actions=actions, tags=[game_tag, "shopping_cart"], image=l.image)
            elif not price_ok and not getattr(self, "gate", None):
                log.info("ignored overpriced %s at %s (%s > MSRP %s)", l.title, name, money(l.price), money(m.msrp))
            if price_ok and not silent:  # startup snapshot isn't a restock; keep it out of the history
                if m.priority >= 4:
                    self.heat(30, f"{l.title[:50]} restocked at {name}")
                self.db.add_event(kind="online_restock", retailer=l.retailer, key=l.key, title=l.title,
                                  price=l.price, game=m.game, watch=m.watch_id)
            if price_ok:
                hot_desc = f"• {label} @ {name} {money(l.price)}" if m.priority >= 4 else None
        elif prev is None and not silent and self.cfg["alerts"]["new_listing_alerts"] and l.status in (COMING_SOON, OUT_OF_STOCK, UNKNOWN) \
                and (m.watch_id or m.priority >= 3) and not l.third_party \
                and self._deal(l, m, price_ok)[0]:
            # new product page at a big retailer = an upcoming drop
            if m.watch_id:
                self.heat(20, f"new listing at {name}")
            self._send(f"newlisting:{l.key}", f"👀 New listing at {name}: {label}"[:200],
                       f"{l.title}\nStatus: {l.status.replace('_', ' ').title()} · {self._price_line(l, m)}"
                       + (f"\n{self._deal(l, m, price_ok)[1]}" if self._deal(l, m, price_ok)[1] else "")
                       + (f"\nRelease: {l.release_date}" if l.release_date else "")
                       + "\nNew product pages usually go live before the drop. Save the link.",
                       priority=max(2, m.priority - 1), url=l.url, actions=actions, tags=[game_tag, "eyes"],
                       cooldown=86400 * 30)
        elif is_buyable(l.status) and prev is not None and self.cfg["alerts"]["below_msrp_alerts"] \
                and self.matcher.below_msrp(l.price, m) and (prev["price"] or 1e9) > (l.price or 0) + 0.5 and not baseline \
                and self._deal(l, m, True)[0]:
            self._send(f"price:{l.key}:{l.price}", f"💲 Below MSRP at {name}: {label}"[:200],
                       f"{l.title}\n{self._price_line(l, m)}\n{self._deal(l, m, True)[1] or value_line}",
                       priority=min(5, m.priority + bump), url=l.url, actions=actions, tags=[game_tag, "moneybag"])
        if changed and prev is not None and not is_buyable(l.status) and is_buyable(prev_status):
            self.db.add_event(kind="online_soldout", retailer=l.retailer, key=l.key, title=l.title, game=m.game,
                              watch=m.watch_id)

        # --- physical stores near you ---
        if l.stores or l.stores_checked:
            self._process_stores(l, m, silent, label, name, value_line, bump, game_tag)
        return hot_desc

    def _process_stores(self, l: Listing, m: Match, silent: bool, label: str, retailer: str,
                        value_line: str, bump: int, game_tag: str) -> None:
        now = time.time()
        prev = self.db.stores_for(l.key)
        restocked: list[tuple[StoreStock, Optional[int]]] = []
        seen_ids = set()
        for s in l.stores:
            if s.distance_miles is not None and s.distance_miles > self.radius:
                continue
            seen_ids.add(s.store_id)
            p = prev.get(s.store_id)
            was = p["status"] if p else None
            was_qty = p["qty"] if p else None
            new_stock = is_buyable(s.status) and (p is None or not is_buyable(was))
            jump = (s.quantity is not None and was_qty is not None and s.quantity - was_qty >= self.qty_jump)
            if (new_stock or jump) and not (p is None and silent):
                restocked.append((s, was_qty))
                self.db.add_event(kind="store_restock", retailer=l.retailer, key=l.key, title=l.title,
                                  store_id=s.store_id, store_name=s.name, qty_before=was_qty, qty_after=s.quantity,
                                  price=l.price, game=m.game, watch=m.watch_id)
            elif p is not None and is_buyable(was) and not is_buyable(s.status):
                self.db.add_event(kind="store_soldout", retailer=l.retailer, key=l.key, title=l.title,
                                  store_id=s.store_id, store_name=s.name, qty_before=was_qty, qty_after=s.quantity,
                                  game=m.game, watch=m.watch_id)
            self.db.x("""INSERT OR REPLACE INTO store_stock (key, store_id, store_name, address, distance, status, qty, last_seen)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (l.key, s.store_id, s.name, s.address, s.distance_miles, s.status, s.quantity, now))
        if l.stores_checked:
            for sid, p in prev.items():
                if sid not in seen_ids and is_buyable(p["status"]):
                    self.db.x("UPDATE store_stock SET status=?, qty=0, last_seen=? WHERE key=? AND store_id=?",
                              (OUT_OF_STOCK, now, l.key, sid))
        if not restocked:
            return
        deal_ok, deal_line = self._deal(l, m, l.trusted_price or self.matcher.price_ok(l.price, m))
        if not deal_ok:
            return
        restocked.sort(key=lambda t: t[0].distance_miles if t[0].distance_miles is not None else 999)
        lines = [l.title if not m.watch_name else label, self._price_line(l, m)]
        for s, before in restocked[:6]:
            qty = f"{s.quantity} in stock" if s.quantity is not None else s.status.replace("_", " ").lower()
            if before is not None and s.quantity is not None and before > 0:
                qty = f"{before} → {s.quantity}"
            dist = f"{s.distance_miles:.1f} mi" if s.distance_miles is not None else ""
            pickup = " · pickup ✅" if s.pickup else ""
            lines.append(f"📍 {s.name} ({dist}) — {qty}{pickup}")
        if len(restocked) > 6:
            lines.append(f"+{len(restocked) - 6} more stores")
        if deal_line:
            lines.append(deal_line)
        elif value_line:
            lines.append(value_line)
        if l.retailer == "target":
            lines.append("Tip: store quantity can go up before staff shelve it. Ask Guest Services, or order "
                         "for pickup to lock it in.")
        nearest = restocked[0][0]
        actions = [view_action("🛒 Order pickup", l.url),
                   view_action("🗺️ Directions", maps_url(nearest.address or nearest.name))]
        n = len(restocked)
        title = f"🏬 IN-STORE: {label} at {nearest.name}" + (f" +{n - 1} more" if n > 1 else "")
        self._send(f"store:{l.key}:{','.join(sorted(s.store_id for s, _ in restocked))}", title[:200], "\n".join(lines),
                   priority=max(2, min(5, max(m.priority, 4) + bump + self._retail_adj(l, m))), url=l.url,
                   actions=actions, tags=[game_tag, "department_store"])

    def _signal(self, s: Signal) -> None:
        if s.quiet:
            self.db.mark_sent(f"signal:{s.key}")
            return
        if s.source == "pokemoncenter" or (s.source == "reddit" and s.priority >= 4) or s.source == "sets":
            self.heat(45, s.title[:60])
        cooldown = 86400 * 30 if s.source == "reddit" else 1800
        tags = s.tags + ([GAME_TAG[s.game]] if s.game in GAME_TAG else [])
        actions = [view_action("Open", s.url)] if s.url else None
        self._send(f"signal:{s.key}", s.title, s.message, priority=s.priority, tags=tags, url=s.url,
                   actions=actions, cooldown=cooldown)

    # ---------------- drop mode ----------------
    # Checking everything every few seconds gets the bot blocked (and then it sees nothing), so it checks
    # fast only when a drop is likely: release-day mornings, right after a watched restock anywhere
    # (drops hit several retailers at once), when the Pokémon Center queue goes up, or a new listing appears.
    HOT_FACTOR = {"target": 0.4, "walmart": 0.4, "bestbuy": 0.5, "shopify": 0.4, "sitesearch": 0.5,
                  "reddit": 0.5, "pokemoncenter": 0.5}
    MIN_INTERVAL = 45.0

    def heat(self, minutes: float, reason: str) -> None:
        until = time.time() + minutes * 60
        if until > getattr(self, "hot_until", 0):
            if getattr(self, "hot_until", 0) < time.time():
                log.warning("⚡ drop mode on for %.0f min: %s", minutes, reason)
            self.hot_until = until
            self.hot_reason = reason

    def is_hot(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        if getattr(self, "hot_until", 0) > time.time():
            return True
        # release days: 12am-1pm, when online drops and store openings happen
        if now.hour < 13 and reports.upcoming_releases(self.watchlist, self.cfg["games"], now.date(), days=0):
            return True
        return False

    def effective_interval(self, s: Source) -> float:
        base = s.interval
        if self.cfg["alerts"].get("drop_mode", True) and self.is_hot():
            return max(self.MIN_INTERVAL, base * self.HOT_FACTOR.get(s.name, 1.0))
        return base

    # ---------------- scheduling ----------------
    def run_source_once(self, src: Source) -> None:
        t0 = time.time()
        try:
            res = src.check()
        except Exception as e:  # noqa: BLE001
            log.warning("%s check failed: %s", src.name, e)
            self._health(src, ok=False, err=str(e))
            return
        self.process(src, res)
        self._health(src, ok=res.complete or bool(res.listings))
        log.info("%s: %d listings, %d signals in %.1fs", src.name, len(res.listings), len(res.signals), time.time() - t0)

    def _health(self, src: Source, ok: bool, err: str = "") -> None:
        k = f"health:{src.name}"
        h = self.db.get(k, {"fails": 0})
        h["fails"] = 0 if ok else h["fails"] + 1
        h["last_ok" if ok else "last_fail"] = time.time()
        if err:
            h["error"] = err[:200]
        self.db.set(k, h)
        # Short blocks are normal (retailers rate-limit, the bot backs off and recovers by itself), so only
        # tell the user when a source has been down for a long stretch, and again when it recovers.
        if h["fails"] and not h.get("warned"):
            h.setdefault("down_since", time.time())
            self.db.set(k, h)
            if time.time() - h["down_since"] >= float(self.cfg["alerts"].get("source_down_hours", 3)) * 3600:
                h["warned"] = True
                self.db.set(k, h)
                self.n.send(f"⚠️ {src.label} has been blocked for a few hours",
                            f"{src.label} keeps refusing the bot ({h.get('error', 'blocked')[:120]}). This is usually "
                            "temporary rate-limiting; the bot backs off and retries on its own. Other stores and "
                            "in-store checks are unaffected. If it lasts more than a day, run "
                            "`.venv\\Scripts\\python -m restockbot doctor` and send the result to Claude.",
                            priority=2, tags=["warning"])
        if ok and "down_since" in h:
            h.pop("down_since")
            self.db.set(k, h)
        if ok and h.get("warned"):
            h["warned"] = False
            self.n.send(f"✅ {src.label} is working again", "Checks are back to normal.", priority=2,
                        tags=["white_check_mark"])
            self.db.set(k, h)

    def run_forever(self) -> bool:
        """Runs until stopped. Returns True when a self-update was installed and a restart is needed."""
        next_run = {s.name: time.time() + i * 5 for i, s in enumerate(self.sources)}
        s_last: dict[str, float] = {}
        running: dict[str, Future] = {}
        log.info("Monitoring %s. Ctrl+C to stop.", ", ".join(s.label for s in self.sources))
        with ThreadPoolExecutor(max_workers=max(1, len(self.sources))) as pool:
            while True:
                now = time.time()
                for s in self.sources:
                    f = running.get(s.name)
                    if f and not f.done():
                        continue
                    if now >= next_run[s.name]:
                        s_last[s.name] = now
                        running[s.name] = pool.submit(self.run_source_once, s)
                        next_run[s.name] = now + self.effective_interval(s) * random.uniform(0.85, 1.15)
                    elif self.is_hot():  # drop mode just started: don't wait out a long normal interval
                        next_run[s.name] = min(next_run[s.name], s_last.get(s.name, now) + self.effective_interval(s))
                try:
                    reports.scheduled(self, datetime.now())
                except Exception:  # noqa: BLE001
                    log.exception("scheduled report failed")
                updater = getattr(self, "updater", None)
                if updater and updater.due() and updater.check_and_apply():
                    log.warning("update installed; restarting after current checks finish")
                    pool.shutdown(wait=True, cancel_futures=True)
                    return True
                time.sleep(5)
