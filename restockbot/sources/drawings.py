"""Retailer drawings (lotteries): Walmart's collectibles drawings and any other drawing pages listed
in watchlist.yaml. Drawings have short entry windows (Walmart: often Wednesdays ~2-3pm PT) and winners
are picked at random afterwards, so the alerts are about the window: posted, opening soon, open now.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..models import CheckResult, Signal
from .base import Source, clean, dig, extract_json_script, to_price, walk

log = logging.getLogger(__name__)

WALMART_DRAW = "https://www.walmart.com/shop/collectibles/draw"
TZ_OFFSETS = {"PDT": -7, "PST": -8, "MDT": -6, "MST": -7, "CDT": -5, "CST": -6, "EDT": -4, "EST": -5,
              "PT": -7, "MT": -6, "CT": -5, "ET": -4}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"], 1)}
WINDOW = re.compile(
    r"(?:entry\s+)?window[^A-Za-z0-9]{0,10}([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s*(?:\d{4},?\s*)?"
    r"(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)\s*(?:-|–|to)\s*(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)\s*([A-Z]{2,3})?", re.I)
CARD_WORDS = re.compile(r"pok[eé]mon|one piece|trading card|tcg|booster|elite trainer", re.I)


def _clock(s: str) -> tuple[int, int]:
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([ap])", s.replace(".", "").strip(), re.I)
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    return (h % 12) + (12 if ap == "p" else 0), mi


def parse_window(text: str, now: Optional[datetime] = None) -> Optional[tuple[datetime, datetime]]:
    """'Drawing entry window Oct 21, 2:00pm - 3:00pm PDT' -> (start, end) as aware UTC datetimes."""
    m = WINDOW.search(re.sub(r"\s+", " ", text))
    if not m or m.group(1).lower()[:3] not in MONTHS:
        return None
    now = now or datetime.now(timezone.utc)
    month, day = MONTHS[m.group(1).lower()[:3]], int(m.group(2))
    tz = timezone(timedelta(hours=TZ_OFFSETS.get((m.group(5) or "PT").upper(), -7)))
    year = now.year
    sh, sm = _clock(m.group(3))
    eh, em = _clock(m.group(4))
    start = datetime(year, month, day, sh, sm, tzinfo=tz)
    if start < now - timedelta(days=60):  # e.g. a January window seen in December
        start = start.replace(year=year + 1)
    end = start.replace(hour=eh, minute=em)
    if end <= start:
        end += timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def parse_items(page: str) -> list[dict]:
    raw = extract_json_script(page, "__NEXT_DATA__")
    out, seen = [], set()
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        for d in walk(data):
            name = d.get("name")
            iid = d.get("usItemId")
            if not iid or not isinstance(name, str) or iid in seen:
                continue
            seen.add(iid)
            price = to_price(dig(d, "priceInfo", "currentPrice", "price")) or to_price(d.get("price"))
            url = d.get("canonicalUrl") or f"/ip/{iid}"
            out.append({"id": str(iid), "name": clean(name), "price": price,
                        "url": url if url.startswith("http") else f"https://www.walmart.com{url}"})
    return out


def local(dt: datetime) -> str:
    t = dt.astimezone()  # the PC's own time zone
    return t.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


class DrawingsSource(Source):
    name = "drawings"
    label = "Drawings"

    def __init__(self, *a, watchlist: dict, **kw):
        super().__init__(*a, **kw)
        self.watchlist = watchlist

    def pages(self) -> list[dict]:
        pages = [{"name": "Walmart", "url": WALMART_DRAW}]
        for p in self.watchlist.get("drawings", []) or []:
            if p.get("url") and p["url"] != WALMART_DRAW:
                pages.append(p)
        return pages

    def check(self) -> CheckResult:
        res = CheckResult()
        now = datetime.now(timezone.utc)
        for p in self.pages():
            try:
                page = self.http.get(p["url"]).text
            except Exception as e:  # noqa: BLE001
                log.info("drawings %s: %s", p["name"], e)
                res.complete = False
                continue
            text = clean(page)
            items = [i for i in parse_items(page) if CARD_WORDS.search(i["name"])]
            window = parse_window(text, now)
            if not items and not CARD_WORDS.search(text):
                continue  # no Pokémon / One Piece in this drawing
            res.signals += self.window_signals(p, window, items, now)
            if window:
                self.ctx.db.set(f"drawing:{p['name']}", {"start": window[0].timestamp(), "end": window[1].timestamp(),
                                                         "items": [i["name"] for i in items][:8], "url": p["url"]})
        return res

    @staticmethod
    def window_signals(p: dict, window, items: list[dict], now: datetime) -> list[Signal]:
        names = "\n".join(f"• {i['name'][:80]}" + (f" (${i['price']:.2f})" if i.get("price") else "")
                          for i in items[:8]) or "• See the drawing page for the items"
        out = []
        tag = window[0].strftime("%Y%m%d%H%M") if window else datetime.now().strftime("%Y%m%d")
        how = ("Sign in, then choose your shipping address, payment card and quantity before entering; "
               "they're locked to the entry. Winners are picked at random after the window closes and charged "
               "automatically.")
        if not window:
            out.append(Signal(source="drawings", key=f"draw:{p['name']}:posted:{tag}",
                              title=f"🎟️ {p['name']} drawing posted",
                              message=f"{names}\nNo entry window found on the page, so check it now.\n{how}",
                              url=p["url"], priority=4, tags=["tickets"]))
            return out
        start, end = window
        when = f"{local(start)} – {local(end).split(', ')[-1]} (your time)"
        if now < start:
            out.append(Signal(source="drawings", key=f"draw:{p['name']}:posted:{tag}",
                              title=f"🎟️ {p['name']} drawing: entry {local(start)}",
                              message=f"Entry window: {when}\n{names}\n{how}",
                              url=p["url"], priority=4, tags=["tickets", "calendar"]))
            if start - now <= timedelta(minutes=20):
                out.append(Signal(source="drawings", key=f"draw:{p['name']}:soon:{tag}",
                                  title=f"⏰ {p['name']} drawing opens in {max(1, int((start - now).seconds / 60))} min",
                                  message=f"Entry window: {when}\n{names}\nGet signed in and set your address and card now.",
                                  url=p["url"], priority=5, tags=["alarm_clock", "tickets"]))
        elif start <= now < end:
            out.append(Signal(source="drawings", key=f"draw:{p['name']}:open:{tag}",
                              title=f"🎟️🚨 {p['name']} drawing OPEN NOW, enter before {local(end).split(', ')[-1]}",
                              message=f"{names}\n{how}",
                              url=p["url"], priority=5, tags=["rotating_light", "tickets"]))
        return out

    def doctor(self) -> str:
        res = self.check()
        if not res.complete and not res.signals:
            return "FAILED: drawing page blocked or unreachable (details in data/restockbot.log)"
        d = self.ctx.db.get("drawing:Walmart")
        if d:
            return (f"OK: Walmart drawing window {local(datetime.fromtimestamp(d['start'], timezone.utc))} "
                    f"with {len(d['items'])} card items")
        return "OK: no Pokémon / One Piece drawing posted right now"
