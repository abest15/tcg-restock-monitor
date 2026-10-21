"""Usage: python -m restockbot <command>

  setup        interactive first-time setup (ZIP, radius, phone alerts)
  run          start monitoring (leave running)
  once         run every source one time and print what it found (no alerts sent)
  doctor       test each data source and report what works from your network
  test-alert   send a test notification to your phone
  stores       list the stores the bot is watching near you
  patterns     show learned in-store restock patterns per store
  calendar     show upcoming releases from the watchlist
  status       show what is buyable right now and recent restocks
  keys         add free Best Buy / Kroger (King Soopers) API keys for more in-store stock
  update       install the latest version now (the bot also does this by itself)
"""
from __future__ import annotations

import logging
import re
import secrets
import sys
import time
from datetime import date, datetime
from logging.handlers import RotatingFileHandler

from .config import CONFIG_PATH, DATA_DIR, ROOT, load_config, load_watchlist
from .db import DB
from .engine import Engine, money, retailer_name
from .http import Http
from .matcher import Matcher
from .notify import Notifier
from .reports import describe_patterns, forecast, store_patterns, upcoming_releases, _release_line
from .sources import REGISTRY, build_sources
from .sources.base import Context
from .sources.sets import SetTracker

log = logging.getLogger("restockbot")


def setup_logging(verbose: bool = False) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = RotatingFileHandler(DATA_DIR / "restockbot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build(dry_run: bool = False, only: list[str] | None = None):
    cfg = load_config()
    wl = load_watchlist()
    db = DB(DATA_DIR / "restockbot.db")
    matcher = Matcher(wl, cfg["games"], cfg["alerts"]["max_price_multiplier"])
    http = Http(cfg["http"]["timeout_seconds"], cfg["http"]["user_agent"])
    ctx = Context(cfg, wl, matcher, db)
    ctx.tracker = SetTracker(ctx, wl)
    ctx.tracker.apply_all()  # sets learned on earlier runs: searches, priorities, calendar
    sources = build_sources(cfg, wl, http, ctx, only=only)
    engine = Engine(cfg, wl, db, matcher, Notifier(cfg, dry_run=dry_run), sources)
    engine.tracker = ctx.tracker
    from .updater import Updater
    engine.updater = Updater(cfg, http, db, engine.n)
    engine.updater.remember_watchlist()
    from .market import MarketLookup, PriceGate
    engine.gate = PriceGate(cfg, MarketLookup(http, db) if cfg["sources"]["tcgplayer"].get("enabled", True) else None)
    return cfg, wl, db, engine


def ask(prompt: str, default: str = "", validate=None) -> str:
    while True:
        v = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip() or default
        if not validate or validate(v):
            return v
        print("  That doesn't look right, try again.")


def cmd_setup() -> None:
    print("\n=== Pokémon & One Piece restock monitor setup ===\n")
    existing = load_config() if CONFIG_PATH.exists() else None
    ex_loc = (existing or {}).get("location", {})
    zip_code = ask("Your 5-digit ZIP code", str(ex_loc.get("zip", "")), lambda v: bool(re.fullmatch(r"\d{5}", v)))
    radius = ask("How many miles will you drive for an in-store restock", str(ex_loc.get("radius_miles", 25)),
                 lambda v: v.isdigit() and 1 <= int(v) <= 100)
    print("\nType the names of your city and nearby towns or areas, comma separated. The bot uses them to spot")
    print("local restock posts on Reddit (e.g. 'Plano, Frisco, McKinney, Allen').")
    towns = [t.strip() for t in ask("Nearby towns", "").split(",") if t.strip()]
    topic = (existing or {}).get("ntfy", {}).get("topic") or f"tcg-restock-{secrets.token_hex(5)}"
    print("\nOptional: Best Buy has a free official API with store-level stock.")
    print("Get a key at https://developer.bestbuy.com (2 minutes), or press Enter to skip for now.")
    bb = ask("Best Buy API key", (existing or {}).get("sources", {}).get("bestbuy", {}).get("api_key", ""))
    print("\nOptional: local card shops that run Shopify (most do). Enter their website domains, comma separated,")
    print("e.g. 'mylocalcardshop.com, anothershop.com'. Press Enter to skip; you can add them later in config.yaml.")
    shops = [s.strip().replace("https://", "").replace("http://", "").strip("/")
             for s in ask("Local shop websites", "").split(",") if s.strip()]

    tmpl = (ROOT / "config.template.yaml").read_text(encoding="utf-8")
    shop_yaml = "".join(f'\n      - name: "{s}"\n        domain: "{s}"' for s in shops) or " []"
    out = (tmpl.replace("__ZIP__", zip_code).replace("__RADIUS__", radius).replace("__TOPIC__", topic)
           .replace("__BESTBUY_ENABLED__", "true" if bb else "false").replace("__BESTBUY_KEY__", bb)
           .replace("__LOCAL_TERMS__", ", ".join(f'"{t}"' for t in towns))
           .replace("__SHOPIFY_ENABLED__", "true" if shops else "false").replace(" __SHOPIFY_STORES__", shop_yaml))
    CONFIG_PATH.write_text(out, encoding="utf-8")
    print(f"\nSaved {CONFIG_PATH}")
    print("\n=== Connect your phone ===")
    print("1. Install the 'ntfy' app (App Store or Google Play).")
    print(f"2. In the app tap '+', subscribe to topic:   {topic}")
    print("   (server: ntfy.sh. Keep this topic name private: anyone who knows it can read your alerts)")
    print("3. iPhone: allow notifications. Android: in the topic settings, set 'Max priority' alerts to")
    print("   override Do Not Disturb if you want urgent drops to wake you up.")
    input("\nPress Enter once you've subscribed, and the bot will send a test alert...")
    cmd_test_alert()
    print("\nNext: run `python -m restockbot doctor` to test the retailers, then `python -m restockbot run`.")


def cmd_keys() -> None:
    """Saves free API keys to keys.yaml (kept out of config.yaml, never touched by updates)."""
    import yaml
    from .config import KEYS_PATH, load_yaml
    keys = load_yaml(KEYS_PATH)
    src = keys.setdefault("sources", {})
    print("Free API keys add more stores with in-store stock. Press Enter to keep the current value.\n")
    print("Best Buy: https://developer.bestbuy.com  (sign up, copy the API key)")
    bb = ask("Best Buy API key", src.get("bestbuy", {}).get("api_key", ""))
    print("\nKroger / King Soopers: https://developer.kroger.com  (create an account, then Register an App;")
    print("  environment: Production, scope: product.compact; copy the Client ID and Client Secret)")
    kid = ask("Kroger Client ID", src.get("kroger", {}).get("client_id", ""))
    ksec = ask("Kroger Client Secret", src.get("kroger", {}).get("client_secret", ""))
    if bb:
        src["bestbuy"] = {"enabled": True, "api_key": bb}
    if kid and ksec:
        src["kroger"] = {"enabled": True, "client_id": kid, "client_secret": ksec}
    KEYS_PATH.write_text(yaml.safe_dump(keys, sort_keys=False), encoding="utf-8")
    print(f"\nSaved to {KEYS_PATH}. Restart the bot (close and reopen start.bat) to use them.")


def cmd_test_alert() -> None:
    cfg = load_config()
    n = Notifier(cfg)
    ok = n.send("🧪 Test alert: restock monitor connected",
                "If you can read this on your phone, alerts work.\nUrgent drops arrive at max priority like this one.",
                priority=5, tags=["pokeball", "white_check_mark"], click="https://ntfy.sh")
    print("Test alert sent. Check your phone." if ok else "Sending failed. Check the ntfy topic in config.yaml.")


def cmd_doctor() -> None:
    cfg, wl, db, engine = build(dry_run=True, only=list(REGISTRY))
    print(f"ZIP {cfg['location']['zip'] or '(not set! run setup)'}, radius {cfg['location']['radius_miles']} mi, "
          f"games: {', '.join(cfg['games'])}\n")
    from .sources import is_enabled
    for s in engine.sources:
        enabled = is_enabled(s.name, cfg["sources"].get(s.name, {}), wl)
        t0 = time.time()
        try:
            msg = s.doctor()
        except Exception as e:  # noqa: BLE001
            msg = f"FAILED: {type(e).__name__}: {str(e)[:160]}"
        print(f"[{'on ' if enabled else 'off'}] {s.label:16} {msg}  ({time.time() - t0:.1f}s)")
    gate = getattr(engine, "gate", None)
    if gate and gate.enabled:
        from .matcher import Match
        t0 = time.time()
        try:
            for title, game in (("Pokemon TCG Mega Evolution Delta Reign Elite Trainer Box", "pokemon"),
                                ("One Piece Card Game The Dominance of God Booster Box OP-18", "onepiece")):
                mk = gate.lookup.find(title, game)
                print(f"[on ] Market value     {game}: " + (f"OK, '{mk.name[:45]}' sells for about {money(mk.market)}"
                      if mk else "not found (alerts still use the retail-price rule)")
                      + f"  ({time.time() - t0:.1f}s)")
        except Exception as e:  # noqa: BLE001
            print(f"[on ] Market value     FAILED: {str(e)[:120]} (alerts still use the retail-price rule)")
    print("\nFAILED/blocked sources keep retrying with backoff while running. Retailers sometimes block")
    print("home connections for a while. Target, Best Buy (API) and Shopify shops are the most reliable.")


def cmd_once() -> None:
    cfg, wl, db, engine = build(dry_run=True)
    for s in engine.sources:
        try:
            res = s.check()
        except Exception as e:  # noqa: BLE001
            print(f"\n== {s.label}: FAILED {e}")
            continue
        print(f"\n== {s.label}: {len(res.listings)} listings, {len(res.signals)} signals")
        for l in res.listings:
            m = engine.matcher.match(l.title)
            if not m:
                continue
            stores = [st for st in l.stores if st.status in ("IN_STOCK", "LIMITED")]
            flag = "" if engine.matcher.price_ok(l.price, m) else "  (above MSRP, ignored)"
            print(f"  {l.status:12} {money(l.price):>9}  {l.title[:70]}{flag}")
            for st in stores[:5]:
                print(f"       📍 {st.name} {st.distance_miles} mi qty={st.quantity}")
        for sig in res.signals:
            print(f"  SIGNAL p{sig.priority}: {sig.title}")


def cmd_stores() -> None:
    cfg, wl, db, engine = build(dry_run=True, only=["target", "bestbuy"])
    for s in engine.sources:
        if s.name == "target":
            for st in s.nearby_stores():
                print(f"  {st['name']:35} {st['distance']:>5} mi  {st['address']}")
    rows = db.q("SELECT DISTINCT store_name, distance, address FROM store_stock ORDER BY distance")
    if rows:
        print("\nStores with recorded stock data:")
        for r in rows:
            print(f"  {r['store_name']:35} {r['distance'] or '?':>5} mi  {r['address']}")


def cmd_patterns() -> None:
    _, _, db, _ = build(dry_run=True, only=[])
    pats = store_patterns(db)
    print(describe_patterns(pats, 30))
    from .reports import online_drop_patterns
    online = online_drop_patterns(db)
    if online:
        print("\nOnline drop timing:\n" + "\n".join(online))
    tomorrow = date.fromordinal(date.today().toordinal() + 1)
    fc = forecast(db, tomorrow)
    if fc:
        print("\nLikely tomorrow: " + ", ".join(p["store"] for p, _ in fc))


def cmd_calendar() -> None:
    cfg, wl, db, engine = build(dry_run=True, only=[])
    rel = upcoming_releases(wl, cfg["games"], date.today(), days=180)
    print("\n".join(_release_line(r) for r in rel) or "No upcoming releases in watchlist.yaml")
    learned = engine.tracker.learned.values()
    if learned:
        print("\nSets the bot discovered on its own:")
        for e in sorted(learned, key=lambda e: e.get("date") or "9999"):
            print(f"  {e['game']:8} {e['name']:35} {e.get('date') or 'date unknown':12} (from {e['source']})")


def cmd_status() -> None:
    _, _, db, _ = build(dry_run=True, only=[])
    live = db.q("SELECT * FROM listings WHERE status IN ('IN_STOCK','LIMITED','PREORDER') ORDER BY game, title")
    print(f"Buyable online right now ({len(live)}):")
    for r in live:
        print(f"  {retailer_name(r['retailer']):14} {money(r['price']):>9}  {r['title'][:70]}")
    stores = db.q("""SELECT s.store_name, s.distance, s.qty, l.title FROM store_stock s JOIN listings l ON l.key=s.key
                     WHERE s.status IN ('IN_STOCK','LIMITED') ORDER BY s.distance""")
    print(f"\nIn stock at stores near you ({len(stores)}):")
    for r in stores[:40]:
        print(f"  {r['store_name'][:28]:28} {r['distance'] or '?':>5} mi qty={r['qty']}  {r['title'][:55]}")
    ev = db.q("SELECT * FROM events WHERE kind IN ('online_restock','store_restock') ORDER BY ts DESC LIMIT 15")
    print("\nRecent restocks:")
    for e in ev:
        where = e["store_name"] or retailer_name(e["retailer"])
        print(f"  {datetime.fromtimestamp(e['ts']):%a %m/%d %I:%M%p}  {where[:28]:28} {e['title'][:55]}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:  # Windows consoles default to a codepage that can't print emoji
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    verbose = "-v" in args
    if cmd in ("help", "-h", "--help"):
        print(__doc__)
        return
    if cmd != "setup":
        setup_logging(verbose)
        if not CONFIG_PATH.exists():
            print("No config.yaml yet. Run:  python -m restockbot setup")
            sys.exit(1)
    commands = {"keys": cmd_keys, "setup": cmd_setup, "test-alert": cmd_test_alert, "doctor": cmd_doctor, "once": cmd_once,
                "stores": cmd_stores, "patterns": cmd_patterns, "calendar": cmd_calendar, "status": cmd_status}
    if cmd == "run":
        _, _, _, engine = build(dry_run="--dry-run" in args)
        if not engine.sources:
            print("No sources enabled in config.yaml")
            sys.exit(1)
        try:
            if engine.run_forever():
                from .updater import RESTART_EXIT_CODE
                sys.exit(RESTART_EXIT_CODE)  # start.bat / the service starts the new version
        except KeyboardInterrupt:
            print("\nStopped.")
        return
    if cmd == "update":
        _, _, _, engine = build()
        from . import __version__
        print(f"Installed: v{__version__}. Checking GitHub...")
        print("Updated. Restart the bot (close and reopen start.bat)." if engine.updater.check_and_apply(force="--force" in args)
              else "No update installed (already current, or see the message above).")
        return
    if cmd not in commands:
        print(__doc__)
        sys.exit(1)
    commands[cmd]()


if __name__ == "__main__":
    main()
