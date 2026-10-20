"""TCGplayer market prices for watched products and your portfolio. Alerts show
resale value next to MSRP so you can see which restocks are worth chasing."""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..config import PORTFOLIO_PATH, load_yaml
from ..models import CheckResult
from .base import Source, to_price

log = logging.getLogger(__name__)
DETAILS = "https://mp-search-api.tcgplayer.com/v1/product/{pid}/details"


def parse_details(data: dict) -> tuple[Optional[float], Optional[float]]:
    d = data.get("results", [data])[0] if isinstance(data.get("results"), list) else data
    return to_price(d.get("marketPrice")), to_price(d.get("lowestPrice"))  # shipping excluded


class TCGplayerSource(Source):
    name = "tcgplayer"
    label = "TCGplayer prices"

    def __init__(self, *a, watchlist: dict, **kw):
        super().__init__(*a, **kw)
        self.watchlist = watchlist

    def product_ids(self) -> dict[str, int]:
        ids = {w["id"]: int(w["tcgplayer_id"]) for w in self.watchlist.get("products", [])
               if w.get("tcgplayer_id") and w.get("id")}
        for h in load_yaml(PORTFOLIO_PATH).get("holdings", []) or []:
            if h.get("tcgplayer_id"):
                ids[f"tcg:{h['tcgplayer_id']}"] = int(h["tcgplayer_id"])
        return ids

    def check(self) -> CheckResult:
        now = time.time()
        ids = self.product_ids()
        ok = 0
        for key, pid in ids.items():
            try:
                market, low = parse_details(self.http.json(DETAILS.format(pid=pid), check_block=False))
            except Exception as e:  # noqa: BLE001
                log.info("tcgplayer %s: %s", pid, e)
                continue
            if market or low:
                self.ctx.db.x("INSERT OR REPLACE INTO prices (product, ts, market, low) VALUES (?,?,?,?)",
                              (key, now, market, low))
                ok += 1
            time.sleep(1.5)
        return CheckResult(complete=ok > 0 or not ids)

    def doctor(self) -> str:
        n = len(self.product_ids())
        self.check()
        rows = self.ctx.db.q("SELECT COUNT(DISTINCT product) c FROM prices WHERE ts > ?", (time.time() - 600,))
        if n and not rows[0]["c"]:
            return "FAILED: could not fetch prices from TCGplayer (alerts still work, without resale values)"
        return f"OK: {n} products with TCGplayer IDs, prices stored for {rows[0]['c']}"


def latest_market(db, product_key: str) -> Optional[float]:
    rows = db.q("SELECT market, low FROM prices WHERE product=? ORDER BY ts DESC LIMIT 1", (product_key,))
    return (rows[0]["market"] or rows[0]["low"]) if rows else None
