from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
WATCHLIST_PATH = ROOT / "watchlist.yaml"
PORTFOLIO_PATH = ROOT / "portfolio.yaml"
DATA_DIR = ROOT / "data"

DEFAULTS: dict[str, Any] = {
    "location": {"zip": "", "radius_miles": 25, "timezone": ""},
    "games": ["pokemon", "onepiece"],
    "ntfy": {"server": "https://ntfy.sh", "topic": "", "token": ""},
    "alerts": {
        "cooldown_minutes": 20,
        "max_price_multiplier": 1.15,      # ignore listings priced above MSRP x this (scalpers)
        "deal_filter": {"enabled": True, "compare": "both", "when_no_tcgplayer_price": "skip", "retail_only": False},
        "below_msrp_alerts": True,
        "new_listing_alerts": True,
        "store_quantity_jump": 3,          # qty increase that counts as an in-store restock
        "quiet_hours": {"enabled": False, "start": "23:00", "end": "07:00", "min_priority": 5},
        "daily_digest_time": "08:00",
        "restock_forecast_time": "20:00",
        "weekly_report_day": "sunday",
    },
    "sources": {
        "target": {"enabled": True, "interval_seconds": 180, "max_products_per_cycle": 25,
                   "search_interval_seconds": 1800, "max_stores": 20, "api_key": ""},
        "bestbuy": {"enabled": False, "interval_seconds": 180, "api_key": ""},
        "walmart": {"enabled": True, "interval_seconds": 300},
        "pokemoncenter": {"enabled": True, "interval_seconds": 120, "product_urls": []},
        "shopify": {"enabled": False, "interval_seconds": 300, "stores": []},
        "pagewatch": {"enabled": False, "interval_seconds": 300, "pages": []},
        "sitesearch": {"enabled": True, "interval_seconds": 900,
                       "sites": ["costco", "samsclub", "dollargeneral", "fivebelow", "acehardware", "dollartree", "familydollar"]},
        "reddit": {
            "enabled": True,
            "interval_seconds": 300,
            "subreddits": ["PKMNTCGDeals", "OnePieceTCG"],
            "local_terms": [],
        },
        "sets": {"enabled": True, "interval_seconds": 43200},
        "tcgplayer": {"enabled": True, "interval_seconds": 21600},
    },
    "http": {"timeout_seconds": 20, "user_agent": ""},
    "updates": {"enabled": True, "repo": "abest15/tcg-restock-monitor", "branch": "main", "check_hours": 6},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = deep_merge(DEFAULTS, load_yaml(path))
    # configs written by v1.0 used Target rates that trip its bot protection; move them to the gentler defaults
    t = cfg["sources"]["target"]
    if t.get("interval_seconds") == 120 and t.get("max_products_per_cycle") == 40:
        t.update(interval_seconds=180, max_products_per_cycle=25)
    if t.get("search_interval_seconds") == 900:
        t["search_interval_seconds"] = 1800
    return cfg


def load_watchlist(path: Path = WATCHLIST_PATH) -> dict:
    data = load_yaml(path)
    data.setdefault("products", [])
    data.setdefault("search_terms", {})
    data.setdefault("generic_msrp", {})
    data.setdefault("releases", [])
    return data
