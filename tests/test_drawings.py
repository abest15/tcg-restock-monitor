import json
from datetime import datetime, timedelta, timezone

from restockbot.sources import drawings as dr

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)  # 11am PDT


def test_parse_window_from_walmart_text():
    s, e = dr.parse_window("Collectibles Drawing Assortment Drawing entry window Oct 21, 2:00pm - 3:00pm PDT", NOW)
    assert s == datetime(2026, 10, 21, 21, 0, tzinfo=timezone.utc)   # 2pm PDT = 21:00 UTC (3pm in Colorado)
    assert e - s == timedelta(hours=1)
    s2, _ = dr.parse_window("Entry window: Jan 7 2 pm – 3 pm PT", datetime(2026, 12, 20, tzinfo=timezone.utc))
    assert s2.year == 2027
    assert dr.parse_window("no window here", NOW) is None


def test_parse_items_and_card_filter():
    nd = {"props": {"pageProps": {"initialData": {"items": [
        {"usItemId": "111", "name": "Pokémon TCG: Mega Evolution Ascended Heroes Elite Trainer Box",
         "priceInfo": {"currentPrice": {"price": 49.97}}, "canonicalUrl": "/ip/x/111"},
        {"usItemId": "222", "name": "NeeDoh Nice Cube Squishy", "price": 9.97}]}}}}
    page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
    items = [i for i in dr.parse_items(page) if dr.CARD_WORDS.search(i["name"])]
    assert len(items) == 1 and items[0]["url"] == "https://www.walmart.com/ip/x/111"


def test_window_alert_stages():
    p = {"name": "Walmart", "url": dr.WALMART_DRAW}
    window = (datetime(2026, 10, 21, 21, 0, tzinfo=timezone.utc), datetime(2026, 10, 21, 22, 0, tzinfo=timezone.utc))
    items = [{"name": "Ascended Heroes ETB", "price": 49.97}]
    early = dr.DrawingsSource.window_signals(p, window, items, NOW)
    assert [s.priority for s in early] == [4] and "entry" in early[0].title
    soon = dr.DrawingsSource.window_signals(p, window, items, window[0] - timedelta(minutes=10))
    assert any(s.title.startswith("⏰") and s.priority == 5 for s in soon)
    live = dr.DrawingsSource.window_signals(p, window, items, window[0] + timedelta(minutes=5))
    assert live[0].title.startswith("🎟️🚨") and live[0].priority == 5
    assert dr.DrawingsSource.window_signals(p, window, items, window[1] + timedelta(minutes=1)) == []


def test_source_check_end_to_end(tmp_path):
    from restockbot.config import DEFAULTS, load_watchlist
    from restockbot.db import DB
    from restockbot.matcher import Matcher
    from restockbot.sources.base import Context
    wl = load_watchlist()
    ctx = Context(DEFAULTS, wl, Matcher(wl, DEFAULTS["games"]), DB(tmp_path / "d.db"))
    day = datetime.now() + timedelta(days=1)
    page = (f"<html><h1>Collectibles Drawing Assortment</h1><p>Drawing entry window {day:%b} {day.day}, "
            "2:00pm - 3:00pm PDT</p><a>Pokemon Ascended Heroes Elite Trainer Box</a></html>")

    class Http:
        def get(self, *a, **k):
            return type("R", (), {"text": page})()
    src = dr.DrawingsSource({}, Http(), ctx, watchlist=wl)
    res = src.check()
    assert res.signals and res.signals[0].url == dr.WALMART_DRAW
    assert ctx.db.get("drawing:Walmart")["start"] > 0
