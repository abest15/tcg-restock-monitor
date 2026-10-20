"""Learns new Pokémon and One Piece sets automatically, so future releases are hunted without
editing the watchlist. Three inputs:

1. pokemontcg.io's public set list (names, series, release dates)
2. the official One Piece Card Game English product list
3. product titles seen at retailers: pre-orders name a new set before it releases

Each new set becomes a high-priority watch entry, gets its own retailer searches, lands in the
release calendar when a date is known, and triggers a "new set" alert.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Optional

from ..matcher import has, norm
from ..models import CheckResult, Signal
from .base import Source, clean, dig

log = logging.getLogger(__name__)

POKEMON_SETS_API = "https://api.pokemontcg.io/v2/sets"
ONEPIECE_PRODUCTS = "https://en.onepiece-cardgame.com/products/"

# titles like "Pokémon TCG: Mega Evolution—Delta Reign Elite Trainer Box" reveal set names
MAIN_SET_PRODUCTS = r"(?:pok[eé]mon center )?(?:elite trainer box|booster bundle|booster box|booster display|build (?:and|&) battle)"
TITLE_SET = re.compile(
    r"(?:pok[eé]mon(?: trading card game| tcg)?)\s*[:\-–—]?\s*"
    r"(?:(?:mega evolution|scarlet (?:and|&) violet|sword (?:and|&) shield)\s*[—–\-:]\s*)?"
    r"([A-Za-z0-9'’ ]{3,40}?)\s+" + MAIN_SET_PRODUCTS, re.I)
OP_PRODUCT = re.compile(
    r"(BOOSTER PACK|EXTRA BOOSTER|PREMIUM BOOSTER|STARTER DECK|ULTRA DECK)\s*[-–]\s*([^\[\]<>]{3,70}?)\s*[-–]?\s*\[((?:OP|EB|PRB|ST)-?\d{1,3})\]",
    re.I)
NOT_SET_NAMES = {"pokemon", "pokemon center", "center", "exclusive", "trading card game", "tcg", "mega evolution", "scarlet and violet",
                 "sword and shield", "collection", "premium", "the", "new", "set of 2", "2 pack", "3 pack"}


def parse_pokemon_sets(data: dict) -> list[dict]:
    out = []
    for s in data.get("data", []) or []:
        name = clean(s.get("name"))
        if not name:
            continue
        d = None
        try:
            d = datetime.strptime(str(s.get("releaseDate", "")).replace("-", "/"), "%Y/%m/%d").date()
        except ValueError:
            pass
        out.append({"game": "pokemon", "name": name, "series": clean(s.get("series")), "date": d, "code": s.get("id", "")})
    return out


def parse_onepiece_products(page: str) -> list[dict]:
    out, seen = [], set()
    for kind, name, code in OP_PRODUCT.findall(re.sub(r"<[^>]+>", " ", page)):
        name = clean(name).strip(" -–").title()
        code = code.upper().replace("OP", "OP-").replace("--", "-") if "-" not in code else code.upper()
        if code in seen or len(name) < 3:
            continue
        seen.add(code)
        out.append({"game": "onepiece", "name": name, "code": code, "kind": kind.title(), "date": None})
    return out


def set_from_title(title: str) -> Optional[str]:
    m = TITLE_SET.search(title or "")
    if not m:
        return None
    name = clean(m.group(1)).strip(" -–—:")
    if norm(name).strip() in NOT_SET_NAMES or len(name) < 4 or not re.search(r"[a-z]", name, re.I):
        return None
    return name.title() if name.islower() or name.isupper() else name


def code_tokens(code: str) -> str:
    """'OP-18' -> 'op 18|op18' (matcher tokens)."""
    n = norm(code).strip()
    return f"{n}|{n.replace(' ', '')}"


class SetTracker:
    """Keeps the learned sets in the database and applies them to the matcher, searches and calendar."""

    def __init__(self, ctx, watchlist: dict):
        self.ctx = ctx
        self.watchlist = watchlist
        self.title_hits: dict[str, int] = {}

    @property
    def learned(self) -> dict[str, dict]:
        return self.ctx.db.get("learned_sets", {}) or {}

    def known(self, game: str, name: str) -> bool:
        n = norm(name)
        if any(n.strip() in norm(t if isinstance(t, str) else t.get("term", "")) for t in
               self.watchlist.get("search_terms", {}).get(game, []) or []):
            return True
        return any(norm(s["name"]) == n and s["game"] == game for s in self.learned.values())

    def add(self, s: dict, source: str) -> Optional[dict]:
        """Returns the set if it's new."""
        if s["game"] not in self.ctx.games or self.known(s["game"], s["name"]):
            return None
        key = f"{s['game']}:{norm(s['name']).strip()}"
        learned = self.learned
        entry = {"game": s["game"], "name": s["name"], "code": s.get("code", ""), "source": source,
                 "date": s["date"].isoformat() if s.get("date") else "", "added": time.time()}
        learned[key] = entry
        self.ctx.db.set("learned_sets", learned)
        self.apply_one(entry)
        return entry

    def apply_all(self) -> None:
        for entry in self.learned.values():
            self.apply_one(entry)

    def apply_one(self, e: dict) -> None:
        game, name = e["game"], e["name"]
        prefix = "pokemon" if game == "pokemon" else "one piece"
        term = f"{prefix} {name.lower()}"
        if term not in self.ctx.search_terms:
            self.ctx.search_plan.append((term, 1))
            self.ctx.search_terms.append(term)
        wid = f"auto:{norm(name).strip().replace(' ', '-')}"
        if not any(w.get("id") == wid for w in self.ctx.matcher.watches):
            tokens = [norm(name).strip() + (f"|{code_tokens(e['code'])}" if game == "onepiece" and e.get("code") else "")]
            if game == "onepiece":
                tokens = ["one piece"] + tokens
            entry = {"id": wid, "game": game, "match": tokens, "priority": "high"}
            # before the broad catch-alls (e.g. "mega evolution"), after the hand-written specific entries
            idx = next((i for i, w in enumerate(self.ctx.matcher.watches) if w.get("id") in
                        ("mega-evolution", "any-pc-etb", "op-any-box")), len(self.ctx.matcher.watches))
            self.ctx.matcher.watches.insert(idx, entry)
        if e.get("date"):
            rel = self.watchlist.setdefault("releases", [])
            if not any(norm(str(r.get("name", ""))).startswith(norm(name)) for r in rel):
                rel.append({"game": game, "date": e["date"], "name": f"{name} (new set, auto-detected)"})

    def learn_from_title(self, title: str, game: str) -> Optional[dict]:
        if game != "pokemon":
            return None
        name = set_from_title(title)
        if not name:
            return None
        k = norm(name)
        self.title_hits[k] = self.title_hits.get(k, 0) + 1
        if self.title_hits[k] < 2:  # need two different products naming it
            return None
        return self.add({"game": "pokemon", "name": name, "date": None}, "retailer listings")


class SetsSource(Source):
    """Checks the set lists twice a day and alerts on new sets."""
    name = "sets"
    label = "New-set finder"

    def __init__(self, *a, tracker: SetTracker, **kw):
        super().__init__(*a, **kw)
        self.tracker = tracker
        self.stats: list[str] = []

    @property
    def interval(self) -> float:
        return float(self.scfg.get("interval_seconds", 43200))

    def check(self) -> CheckResult:
        res = CheckResult()
        self.stats = []
        first_run = not self.ctx.db.get("sets_primed", False)
        found: list[tuple[dict, str]] = []
        if "pokemon" in self.ctx.games:
            try:
                data = self.http.json(POKEMON_SETS_API, params={"orderBy": "-releaseDate", "pageSize": 40,
                                                                "select": "id,name,series,releaseDate"},
                                      check_block=False)
                sets = parse_pokemon_sets(data)
                cutoff = date.today() - timedelta(days=730)  # last two years + anything announced ahead
                recent = [s for s in sets if s["date"] is None or s["date"] >= cutoff]
                found += [(s, "pokemontcg.io") for s in recent]
                self.stats.append(f"Pokémon set list: {len(sets)} sets ({len(recent)} recent)")
            except Exception as e:  # noqa: BLE001
                res.complete = False
                self.stats.append(f"Pokémon set list: FAILED {str(e)[:80]}")
        if "onepiece" in self.ctx.games:
            try:
                page = self.http.get(ONEPIECE_PRODUCTS, check_block=False).text
                ops = parse_onepiece_products(page)
                found += [(s, "onepiece-cardgame.com") for s in ops]
                self.stats.append(f"One Piece product list: {len(ops)} products")
            except Exception as e:  # noqa: BLE001
                res.complete = False
                self.stats.append(f"One Piece product list: FAILED {str(e)[:80]}")
        new = [e for e in (self.tracker.add(s, src) for s, src in found) if e]
        if not first_run:
            for e in new:
                res.signals.append(new_set_signal(e))
        if found:
            self.ctx.db.set("sets_primed", True)
        self.stats.append(f"{len(self.tracker.learned)} sets learned so far, {len(new)} new this check")
        return res

    def doctor(self) -> str:
        self.check()
        return "OK: " + " | ".join(self.stats) if not any("FAILED" in s for s in self.stats) else \
            "PARTIAL: " + " | ".join(self.stats)


def new_set_signal(e: dict) -> Signal:
    when = f" Releases {e['date']}." if e.get("date") else ""
    game = "Pokémon" if e["game"] == "pokemon" else "One Piece"
    return Signal(source="sets", key=f"newset:{e['game']}:{norm(e['name']).strip()}",
                  title=f"🆕 New {game} set detected: {e['name']}",
                  message=f"Found via {e['source']}.{when} The bot now searches for it at every retailer and "
                          "alerts at high priority. Watch for pre-orders.",
                  priority=4, tags=["new", "pokeball" if e["game"] == "pokemon" else "pirate_flag"],
                  game=e["game"])
