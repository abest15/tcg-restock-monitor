from __future__ import annotations

import html
import logging
import re
from collections import deque
from typing import Any, Iterator, Optional

from ..http import Http
from ..matcher import Matcher
from ..models import CheckResult

log = logging.getLogger(__name__)


class Context:
    """Everything a source needs to know about the user."""

    def __init__(self, cfg: dict, watchlist: dict, matcher: Matcher, db):
        self.cfg = cfg
        self.zip = str(cfg["location"]["zip"]).strip()
        self.radius = float(cfg["location"]["radius_miles"])
        self.games = cfg["games"]
        self.matcher = matcher
        self.db = db
        terms = watchlist.get("search_terms", {})
        # entries are "term" or {term: ..., pages: N}; broad terms get several result pages
        self.search_plan: list[tuple[str, int]] = []
        for g in self.games:
            for t in terms.get(g, []) or []:
                if isinstance(t, dict):
                    self.search_plan.append((str(t["term"]), int(t.get("pages", 1))))
                else:
                    self.search_plan.append((str(t), 1))
        self.search_terms: list[str] = [t for t, _ in self.search_plan]
        self.tracker = None  # SetTracker, attached in build()


class Source:
    name = "base"
    label = "Base"

    def __init__(self, scfg: dict, http: Http, ctx: Context):
        self.scfg = scfg
        self.http = http
        self.ctx = ctx

    @property
    def interval(self) -> float:
        return float(self.scfg.get("interval_seconds", 300))

    def check(self) -> CheckResult:
        raise NotImplementedError

    def doctor(self) -> str:
        """Run one check and describe the result in a sentence."""
        res = self.check()
        if not res.complete and not res.listings and not res.signals:
            return "FAILED: blocked or unreachable from this network (details in data/restockbot.log)"
        n_match = sum(1 for l in res.listings if self.ctx.matcher.match(l.title))
        stores = sum(len(l.stores) for l in res.listings)
        partial = "" if res.complete else " (some requests failed)"
        return (f"OK{partial}: {len(res.listings)} listings ({n_match} match your watchlist), "
                f"{stores} store-stock rows, {len(res.signals)} signals")


# ---------- parsing helpers shared by sources ----------

def walk(obj: Any) -> Iterator[dict]:
    """Yield every dict nested anywhere inside obj, in document order (breadth-first)."""
    queue = deque([obj])
    while queue:
        cur = queue.popleft()
        if isinstance(cur, dict):
            yield cur
            queue.extend(cur.values())
        elif isinstance(cur, list):
            queue.extend(cur)


def dig(obj: Any, *path: Any, default: Any = None) -> Any:
    for p in path:
        if isinstance(obj, dict):
            obj = obj.get(p)
        elif isinstance(obj, list) and isinstance(p, int) and -len(obj) <= p < len(obj):
            obj = obj[p]
        else:
            return default
        if obj is None:
            return default
    return obj


def to_price(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    m = re.search(r"\d[\d,]*\.?\d*", str(v))
    return float(m.group(0).replace(",", "")) if m else None


def clean(text: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))).strip()


def extract_json_script(page: str, script_id: str) -> Optional[str]:
    m = re.search(rf'<script[^>]*id="{re.escape(script_id)}"[^>]*>(.*?)</script>', page, re.S)
    return m.group(1) if m else None
