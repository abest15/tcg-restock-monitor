"""End-to-end: feed the engine a sequence of retailer snapshots and check which alerts fire."""
import time
from datetime import date, datetime, timedelta

import pytest

from restockbot import reports
from restockbot.config import DEFAULTS, deep_merge, load_watchlist
from restockbot.db import DB
from restockbot.engine import Engine
from restockbot.matcher import Matcher
from restockbot.models import COMING_SOON, IN_STOCK, OUT_OF_STOCK, CheckResult, Listing, Signal, StoreStock
from restockbot.notify import Notifier, in_quiet_hours


class Capture(Notifier):
    def __init__(self, cfg):
        super().__init__(cfg, dry_run=True)
        self.sent = []

    def send(self, title, message, **kw):
        self.sent.append((title, message, kw))
        return True


class FakeSource:
    name = "target"
    label = "Target"
    interval = 60

    def __init__(self):
        self.next = CheckResult()

    def check(self):
        return self.next


ETB = "Pokémon Trading Card Game: Mega Evolution—Delta Reign Elite Trainer Box"


def listing(status=OUT_OF_STOCK, price=49.99, stores=None, sku="1", title=ETB, new=True, checked=True):
    return Listing(retailer="target", sku=sku, title=title, url=f"https://target.com/p/A-{sku}", status=status,
                   price=price, stores=stores or [], stores_checked=checked, new_discovery=new)


def store(sid="1375", status=OUT_OF_STOCK, qty=0, dist=2.4):
    return StoreStock(store_id=sid, name=f"Target #{sid}", status=status, quantity=qty, distance_miles=dist,
                      address="1 Main St, Springfield, IL", pickup=status == IN_STOCK)


@pytest.fixture
def env(tmp_path):
    cfg = deep_merge(DEFAULTS, {"location": {"zip": "62701", "radius_miles": 25}})
    wl = load_watchlist()
    db = DB(tmp_path / "t.db")
    n = Capture(cfg)
    src = FakeSource()
    eng = Engine(cfg, wl, db, Matcher(wl, cfg["games"]), n, [src])
    return eng, src, n


def run(eng, src, *listings, signals=()):
    src.next = CheckResult(listings=list(listings), signals=list(signals))
    eng.run_source_once(src)


def titles(n):
    return [t for t, _, _ in n.sent]


def test_baseline_then_online_restock(env):
    eng, src, n = env
    run(eng, src, listing(OUT_OF_STOCK, stores=[store()]))
    assert len(n.sent) == 1 and "monitor is live" in n.sent[0][0]
    run(eng, src, listing(OUT_OF_STOCK, stores=[store()]))
    assert len(n.sent) == 1                              # nothing changed
    run(eng, src, listing(IN_STOCK, stores=[store()]))
    t, msg, kw = n.sent[-1]
    assert "RESTOCK at Target" in t and "Delta Reign Elite Trainer Box" in t
    assert kw["priority"] == 5 and kw["actions"][0]["url"].endswith("A-1")
    assert "MSRP $49.99" in msg


def test_in_store_restock_groups_stores_and_ignores_far(env):
    eng, src, n = env
    run(eng, src, listing(stores=[store("1"), store("2", dist=8.0), store("3", dist=40)]))
    run(eng, src, listing(stores=[store("1", IN_STOCK, 6), store("2", IN_STOCK, 2, dist=8.0), store("3", IN_STOCK, 9, dist=40)]))
    t, msg, kw = n.sent[-1]
    assert t.startswith("🏬 IN-STORE") and "+1 more" in t
    assert "Target #1 (2.4 mi) — 6 in stock" in msg and "Target #3" not in msg
    assert kw["actions"][1]["label"].startswith("🗺️") and "google.com/maps" in kw["actions"][1]["url"]
    ev = eng.db.q("SELECT COUNT(*) c FROM events WHERE kind='store_restock'")[0]["c"]
    assert ev == 2


def test_quantity_jump_counts_as_restock(env):
    eng, src, n = env
    run(eng, src, listing(stores=[store("1", IN_STOCK, 1)]))
    run(eng, src, listing(stores=[store("1", IN_STOCK, 2)]))
    assert len(n.sent) == 1
    run(eng, src, listing(stores=[store("1", IN_STOCK, 8)]))
    assert "2 → 8" in n.sent[-1][1]


def test_store_dropping_out_of_snapshot_marks_sold_out(env):
    eng, src, n = env
    run(eng, src, listing(stores=[store("1", IN_STOCK, 3)]))
    run(eng, src, listing(stores=[]))                     # complete snapshot without store 1
    assert eng.db.stores_for("target:1")["1"]["status"] == OUT_OF_STOCK
    run(eng, src, listing(stores=[store("1", IN_STOCK, 4)]))
    assert n.sent[-1][0].startswith("🏬 IN-STORE")


def test_scalper_price_is_ignored(env):
    eng, src, n = env
    run(eng, src, listing(OUT_OF_STOCK, price=129.99))
    run(eng, src, listing(IN_STOCK, price=129.99))
    assert len(n.sent) == 1


def test_new_listing_alert_and_known_items_silent(env):
    eng, src, n = env
    run(eng, src, listing())
    run(eng, src, listing(), listing(COMING_SOON, sku="9", title="Pokemon TCG 30th Celebration Ultra-Premium Collection", price=179.99))
    assert "New listing at Target" in n.sent[-1][0]
    count = len(n.sent)
    # item known since startup but processed for the first time later: no alert even if in stock
    run(eng, src, listing(), listing(IN_STOCK, sku="10", title="Pokemon TCG Some Set Booster Bundle", new=False,
                                     stores=[store("1", IN_STOCK, 5)]))
    assert len(n.sent) == count


def test_cooldown_and_flip_flop(env):
    eng, src, n = env
    run(eng, src, listing())
    run(eng, src, listing(IN_STOCK))
    run(eng, src, listing(OUT_OF_STOCK))
    run(eng, src, listing(IN_STOCK))                       # within cooldown: no duplicate
    assert sum("RESTOCK" in t for t in titles(n)) == 1


def test_signal_dedupe(env):
    eng, src, n = env
    sig = Signal(source="pokemoncenter", key="q1", title="Pokémon Center queue is UP", message="go", priority=5)
    run(eng, src, listing(), signals=[sig])
    run(eng, src, listing(), signals=[sig])
    assert titles(n).count("Pokémon Center queue is UP") == 1


def test_patterns_and_forecast(env):
    eng, src, n = env
    base = datetime(2026, 9, 1, 7, 30)  # a Tuesday
    for w in range(4):
        eng.db.add_event(kind="store_restock", retailer="target", key="k", title="ETB", store_id="1",
                         store_name="Target Midtown", ts=(base + timedelta(weeks=w)).timestamp())
    pats = reports.store_patterns(eng.db, days=3650)
    assert pats[0]["weekdays"]["Tuesday"] == 4
    assert "morning" in reports.describe_patterns(pats)
    assert reports.forecast(eng.db, date(2026, 10, 6), weeks=520)[0][0]["store"] == "Target Midtown"


def test_scheduled_reports(env):
    eng, src, n = env
    eng.watchlist["releases"] = [{"game": "pokemon", "date": "2026-11-06", "name": "Delta Reign"}]
    reports.scheduled(eng, datetime(2026, 11, 6, 8, 5))
    got = titles(n)
    assert any("Releasing today" in t for t in got) and any("Daily TCG brief" in t for t in got)
    before = len(n.sent)
    reports.scheduled(eng, datetime(2026, 11, 6, 9, 0))
    assert len(n.sent) == before                           # once per day


def test_quiet_hours():
    assert in_quiet_hours(datetime(2026, 1, 1, 23, 30), "23:00", "07:00")
    assert in_quiet_hours(datetime(2026, 1, 1, 3, 0), "23:00", "07:00")
    assert not in_quiet_hours(datetime(2026, 1, 1, 12, 0), "23:00", "07:00")


def test_quiet_signals_are_recorded_not_sent(env):
    eng, src, n = env
    old = Signal(source="reddit", key="reddit:old", title="old post", message="x", quiet=True)
    run(eng, src, listing(), signals=[old])
    run(eng, src, listing(), signals=[Signal(source="reddit", key="reddit:old", title="old post", message="x")])
    assert "old post" not in titles(n)


def test_costco_bundle_not_treated_as_scalper(env):
    eng, src, n = env
    t = "Pokemon TCG Delta Reign Elite Trainer Box 3-Pack"
    mk = lambda st: Listing(retailer="costco", sku="c1", title=t, url="https://costco.com/x", status=st,
                            price=139.99, seller="Costco", trusted_price=True)
    run(eng, src, mk(OUT_OF_STOCK))
    run(eng, src, mk(IN_STOCK))
    assert "RESTOCK at Costco" in n.sent[-1][0]


def test_unknown_status_new_listing_alert(env):
    eng, src, n = env
    run(eng, src, listing())
    run(eng, src, listing(), Listing(retailer="acehardware", sku="a1", title="Pokemon Trading Card Game Booster Pack",
                                     url="https://acehardware.com/p", status="UNKNOWN", seller="Ace Hardware", trusted_price=True))
    assert "New listing at Ace Hardware" in n.sent[-1][0]


def test_online_drop_patterns(env):
    eng, src, n = env
    base = datetime(2026, 9, 1, 21, 5)  # Tuesday 9pm
    for w in range(4):
        eng.db.add_event(kind="online_restock", retailer="walmart", key="k", title="ETB",
                         ts=(base + timedelta(weeks=w)).timestamp())
    lines = reports.online_drop_patterns(eng.db, days=3650)
    assert lines == ["• Walmart: 4 drops, most often Tue (4x), usually around 9pm"]


def test_drop_mode_speeds_up_checks(env):
    eng, src, n = env
    src.interval = 300
    src.name = "walmart"
    eng.watchlist["releases"] = []
    assert eng.effective_interval(src) == 300
    run(eng, src, listing())
    run(eng, src, listing(IN_STOCK))          # watched (max priority) restock -> drop mode
    assert eng.is_hot() and eng.effective_interval(src) == 120
    eng.hot_until = 0
    eng.watchlist["releases"] = [{"game": "pokemon", "date": date.today().isoformat(), "name": "Big set"}]
    assert eng.is_hot(datetime.combine(date.today(), datetime.min.time()).replace(hour=8))
    assert not eng.is_hot(datetime.combine(date.today(), datetime.min.time()).replace(hour=15))
