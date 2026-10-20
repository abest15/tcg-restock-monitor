"""Scheduled pushes: daily digest, release reminders, tomorrow's in-store restock forecast
(learned from history), and a weekly market/portfolio report."""
from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from .config import PORTFOLIO_PATH, load_yaml

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _hhmm_passed(now: datetime, hhmm: str) -> bool:
    h, m = map(int, hhmm.split(":"))
    return (now.hour, now.minute) >= (h, m)


def _once_per_day(db, name: str, now: datetime, hhmm: str) -> bool:
    k = f"sched:{name}"
    today = now.strftime("%Y-%m-%d")
    if db.get(k) == today or not _hhmm_passed(now, hhmm):
        return False
    db.set(k, today)
    return True


# ---------------- in-store restock patterns ----------------
def _bucket(hour: int) -> str:
    if hour < 6:
        return "overnight (12–6am)"
    if hour < 11:
        return "morning (6–11am)"
    if hour < 16:
        return "midday (11am–4pm)"
    return "evening (4pm+)"


def store_patterns(db, days: int = 60) -> list[dict]:
    """For each store: how many separate restock days we've seen, which weekdays, which times."""
    since = time.time() - days * 86400
    rows = db.q("""SELECT ts, retailer, COALESCE(store_id, store_name) AS store_id, store_name, title FROM events
                   WHERE kind IN ('store_restock', 'vending_report') AND ts>=?""", (since,))
    by_store: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_store[(r["retailer"], r["store_id"], r["store_name"])].append(r)
    out = []
    for (retailer, sid, name), evs in by_store.items():
        restock_days = {}
        for e in evs:
            dt = datetime.fromtimestamp(e["ts"])
            restock_days.setdefault(dt.date(), dt)  # first detection that day
        weekdays = Counter(DAYS[d.weekday()] for d in restock_days)
        times = Counter(_bucket(dt.hour) for dt in restock_days.values())
        products = Counter(e["title"] for e in evs)
        out.append({"retailer": retailer, "store_id": sid, "store": name, "restock_days": len(restock_days),
                    "weekdays": weekdays, "times": times, "products": products,
                    "last": max(restock_days.values())})
    out.sort(key=lambda s: -s["restock_days"])
    return out


def online_drop_patterns(db, days: int = 60) -> list[str]:
    """When each retailer tends to drop online: most common weekday and hour of detected restocks."""
    rows = db.q("SELECT ts, retailer FROM events WHERE kind='online_restock' AND ts>=?", (time.time() - days * 86400,))
    by: dict[str, list[datetime]] = defaultdict(list)
    for r in rows:
        by[r["retailer"]].append(datetime.fromtimestamp(r["ts"]))
    out = []
    for retailer, times in sorted(by.items(), key=lambda kv: -len(kv[1])):
        if len(times) < 3:
            continue
        day, n_day = Counter(DAYS[t.weekday()] for t in times).most_common(1)[0]
        hour, _ = Counter(t.hour for t in times).most_common(1)[0]
        ampm = f"{hour % 12 or 12}{'am' if hour < 12 else 'pm'}"
        name = retailer[5:] if retailer.startswith("shop:") else retailer.title()
        out.append(f"• {name}: {len(times)} drops, most often {day[:3]} ({n_day}x), usually around {ampm}")
    return out[:8]


def health_lines(engine: "Engine") -> list[str]:
    out = []
    for s in engine.sources:
        h = engine.db.get(f"health:{s.name}", {}) or {}
        if h.get("fails"):
            out.append(f"• {s.label}: failing ({h.get('error', 'blocked')[:60]})")
    return out


def describe_patterns(pats: list[dict], limit: int = 10) -> str:
    if not pats:
        return ("No in-store restocks recorded yet. Patterns show up after a few weeks of monitoring.")
    lines = []
    for p in pats[:limit]:
        wd = ", ".join(f"{d[:3]} ×{n}" for d, n in p["weekdays"].most_common(3))
        tm = p["times"].most_common(1)[0][0]
        lines.append(f"• {p['store']}: {p['restock_days']} restock days. Usually {wd}, detected {tm}. "
                     f"Last: {p['last']:%a %b %d}")
    return "\n".join(lines)


def forecast(db, day: date, min_hits: int = 2, weeks: int = 8) -> list[tuple[dict, int]]:
    """Stores that restocked on this weekday at least `min_hits` times in recent weeks."""
    wd = DAYS[day.weekday()]
    return [(p, p["weekdays"][wd]) for p in store_patterns(db, days=weeks * 7) if p["weekdays"][wd] >= min_hits]


# ---------------- release calendar ----------------
def upcoming_releases(watchlist: dict, games: list[str], today: date, days: int = 14) -> list[dict]:
    out = []
    for r in watchlist.get("releases", []):
        if r.get("game", "pokemon") not in games:
            continue
        try:
            d = r["date"] if isinstance(r["date"], date) else datetime.strptime(str(r["date"]), "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if today <= d <= today + timedelta(days=days):
            out.append({**r, "date": d, "in_days": (d - today).days})
    return sorted(out, key=lambda r: r["date"])


def _release_line(r: dict) -> str:
    when = "TODAY" if r["in_days"] == 0 else ("tomorrow" if r["in_days"] == 1 else f"{r['date']:%a %b %d}")
    kind = r.get("kind", "release")
    return f"• {when}: {r['name']}" + (f" ({kind})" if kind != "release" else "") + (f": {r['notes']}" if r.get("notes") else "")


# ---------------- portfolio / market ----------------
def portfolio_summary(db) -> str:
    from .sources.tcgplayer import latest_market
    holdings = load_yaml(PORTFOLIO_PATH).get("holdings", []) or []
    if not holdings:
        return ""
    cost = value = 0.0
    lines = []
    for h in holdings:
        qty = int(h.get("quantity", 1))
        paid = float(h.get("paid_each", 0)) * qty
        mk = latest_market(db, f"tcg:{h['tcgplayer_id']}") if h.get("tcgplayer_id") else None
        cost += paid
        if mk:
            value += mk * qty
            pct = (mk * qty - paid) / paid * 100 if paid else 0
            lines.append(f"• {h['name']} ×{qty}: ${mk * qty:,.0f} ({pct:+.0f}%)")
        else:
            value += paid
            lines.append(f"• {h['name']} ×{qty}: no price yet")
    gain = value - cost
    return (f"Portfolio: ${value:,.0f} vs ${cost:,.0f} paid ({gain:+,.0f}, "
            f"{(gain / cost * 100 if cost else 0):+.1f}%)\n" + "\n".join(lines[:15]))


def watch_market_lines(engine: "Engine") -> list[str]:
    from .sources.tcgplayer import latest_market
    out = []
    for w in engine.watchlist.get("products", []):
        if not w.get("id"):
            continue
        mk = latest_market(engine.db, w["id"])
        if mk and w.get("msrp"):
            out.append((mk / w["msrp"], f"• {w.get('name') or w['id']}: ${mk:,.0f} vs ${w['msrp']:.0f} MSRP ({mk / w['msrp']:.1f}x)"))
    return [line for _, line in sorted(out, reverse=True)[:10]]


# ---------------- the scheduler hook ----------------
def scheduled(engine: "Engine", now: datetime) -> None:
    db, a = engine.db, engine.cfg["alerts"]
    games = engine.cfg["games"]

    # release-day reminder at 7am, so you are up for the drop
    if _once_per_day(db, "release_morning", now, "07:00"):
        today = upcoming_releases(engine.watchlist, games, now.date(), days=0)
        if today:
            engine.n.send("📦 Releasing today", "\n".join(_release_line(r) for r in today)
                          + "\nCheck Target/Walmart/Best Buy apps this morning. Online drops often go live overnight "
                            "or early morning, and stores may put stock out at opening.",
                          priority=4, tags=["calendar", "package"])

    if _once_per_day(db, "digest", now, a["daily_digest_time"]):
        parts = []
        rel = upcoming_releases(engine.watchlist, games, now.date(), days=10)
        if rel:
            parts.append("Upcoming:\n" + "\n".join(_release_line(r) for r in rel[:8]))
        since = time.time() - 86400
        ev = db.q("SELECT kind, COUNT(*) c FROM events WHERE ts>=? GROUP BY kind", (since,))
        counts = {r["kind"]: r["c"] for r in ev}
        if counts:
            parts.append(f"Last 24h: {counts.get('online_restock', 0)} online restocks, "
                         f"{counts.get('store_restock', 0)} in-store restocks near you")
        live = db.q("""SELECT title, retailer, price FROM listings WHERE status IN ('IN_STOCK','LIMITED','PREORDER')
                       AND watch != '' AND last_seen >= ? ORDER BY last_change DESC LIMIT 6""", (since,))
        if live:
            parts.append("Buyable now:\n" + "\n".join(f"• {r['title'][:60]} @ {r['retailer']} ${r['price'] or 0:.2f}" for r in live))
        fc = forecast(db, now.date())
        if fc:
            parts.append("Likely in-store restocks today:\n" + "\n".join(f"• {p['store']} (seen {n} recent {DAYS[now.weekday()]}s)" for p, n in fc[:5]))
        if parts:
            engine.n.send(f"☀️ Daily TCG brief, {now:%a %b %d}", "\n\n".join(parts), priority=2, tags=["sunny"])

    if _once_per_day(db, "forecast", now, a["restock_forecast_time"]):
        tomorrow = now.date() + timedelta(days=1)
        fc = forecast(db, tomorrow)
        rel = upcoming_releases(engine.watchlist, games, tomorrow, days=0)
        lines = [f"• {p['store']}: restocked {n} of the recent {DAYS[tomorrow.weekday()]}s, "
                 f"usually detected {p['times'].most_common(1)[0][0]}" for p, n in fc[:6]]
        lines += [_release_line({**r, "in_days": 1}) for r in rel]
        if lines:
            engine.n.send(f"🔮 Tomorrow ({DAYS[tomorrow.weekday()]}): likely restocks",
                          "\n".join(lines) + "\nPlan a stop at opening.", priority=3, tags=["crystal_ball"])

    if now.strftime("%A").lower() == str(a["weekly_report_day"]).lower() and _once_per_day(db, "weekly", now, "18:00"):
        parts = ["In-store restock patterns (last 60 days):\n" + describe_patterns(store_patterns(db), 6)]
        online = online_drop_patterns(db)
        if online:
            parts.append("Online drop timing:\n" + "\n".join(online))
        bad = health_lines(engine)
        if bad:
            parts.append("Sources needing attention:\n" + "\n".join(bad))
        mk = watch_market_lines(engine)
        if mk:
            parts.append("Resale vs MSRP (TCGplayer):\n" + "\n".join(mk))
        pf = portfolio_summary(db)
        if pf:
            parts.append(pf)
        engine.n.send("📊 Weekly TCG report", "\n\n".join(parts), priority=2, tags=["bar_chart"])
