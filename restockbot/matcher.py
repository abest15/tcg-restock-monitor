"""Decides whether a retailer listing is something you care about, and what it should cost."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

PRIORITY = {"max": 5, "high": 4, "normal": 3, "low": 2}

GAME_RULES = {
    "pokemon": {
        "require_all": ["pokemon"],
        "require_any": ["trading card", "tcg", "booster", "elite trainer", "etb", "card game",
                        "premium collection", "blister", "build and battle", "battle deck",
                        "binder collection", "mini tin", "ex box"],
    },
    "onepiece": {
        "require_all": ["one piece"],
        "require_any": ["card game", "tcg", "booster", "starter deck", "double pack", "don card"],
    },
}

NON_TCG = ["plush", "funko", "lego", "nintendo switch", "video game", "costume", "t shirt", "tee shirt",
           "hoodie", "backpack", "squishmallow", "pajama", "mega construx", "card sleeves",
           "deck box", "playmat", "toploader", "9 pocket", "portfolio", "manga", "dvd", "blu ray",
           "lunch box", "puzzle", "calendar", "sticker book", "coloring", "keychain", "action figure",
           "battle fig", "battle figure", "figure multipack", "clip n go", "plushie", "nanoblock"]

# Ordered most-specific first: (game, phrase alternatives, product type, typical US MSRP cap)
PRODUCT_TYPES = [
    ("pokemon", ["pokemon center elite trainer"], "Pokémon Center ETB", 59.99),
    ("pokemon", ["elite trainer", "etb"], "Elite Trainer Box", 49.99),
    ("pokemon", ["ultra premium", "ultrapremium"], "Ultra-Premium Collection", 179.99),
    ("pokemon", ["super premium"], "Super-Premium Collection", 99.99),
    ("pokemon", ["booster bundle"], "Booster Bundle", 26.94),
    ("pokemon", ["booster box", "booster display", "36 pack", "36 booster"], "Booster Box", 161.64),
    ("pokemon", ["binder collection"], "Binder Collection", 31.99),
    ("pokemon", ["premium collection", "premium figure"], "Premium Collection", 69.99),
    ("pokemon", ["figure collection"], "Figure Collection", 29.99),
    ("pokemon", ["build and battle", "build battle"], "Build & Battle", 21.99),
    ("pokemon", ["battle deck"], "Battle Deck", 19.99),
    ("pokemon", ["ex box"], "ex Box", 24.99),
    ("pokemon", ["mini tin"], "Mini Tin", 9.99),
    ("pokemon", ["tin"], "Tin", 29.99),
    ("pokemon", ["3 pack", "three pack", "3 booster", "3pk"], "3-Pack Blister", 15.99),
    ("pokemon", ["2 pack", "two pack", "blister"], "Blister", 9.99),
    ("pokemon", ["collection", "box"], "Collection", 59.99),
    ("pokemon", ["booster pack", "sleeved booster"], "Booster Pack", 5.49),
    ("onepiece", ["extra booster"], "Extra Booster Box", 119.76),
    ("onepiece", ["premium booster"], "Premium Booster", None),
    ("onepiece", ["booster box", "display", "24 pack"], "Booster Box", 119.76),
    ("onepiece", ["double pack"], "Double Pack Set", 19.99),
    ("onepiece", ["starter deck", "ultra deck"], "Starter Deck", 17.99),
    ("onepiece", ["gift collection", "gift box", "illustration box", "collection"], "Collection", 59.99),
    ("onepiece", ["booster pack", "booster"], "Booster Pack", 5.49),
]


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ").replace("—", " ").replace("–", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return f" {re.sub(r' +', ' ', text).strip()} "


def has(ntext: str, phrase: str) -> bool:
    return norm(phrase) in ntext


def has_token(ntext: str, token: str) -> bool:
    """token may be 'a|b|c' meaning any of the alternatives."""
    return any(has(ntext, alt) for alt in token.split("|"))


@dataclass
class Match:
    game: str
    product_type: str
    msrp: Optional[float]
    priority: int
    watch_id: str = ""
    watch_name: str = ""
    tcgplayer_id: Optional[int] = None

    @property
    def label(self) -> str:
        return self.watch_name or self.product_type


class Matcher:
    def __init__(self, watchlist: dict, games: list[str], max_price_multiplier: float = 1.15):
        self.watches = [w for w in watchlist.get("products", []) if w.get("game", "pokemon") in games
                        and w.get("enabled", True)]
        self.games = games
        self.mult = max_price_multiplier
        self.ignore = [norm(x) for x in watchlist.get("ignore", [])]
        generic = watchlist.get("generic_alerts", {})
        self.generic_priority = PRIORITY.get(str(generic.get("priority", "normal")), 3)
        self.generic_enabled = generic.get("enabled", True)

    def detect_game(self, ntext: str) -> Optional[str]:
        for game in self.games:
            rule = GAME_RULES.get(game)
            if not rule:
                continue
            if all(has(ntext, p) for p in rule["require_all"]) and any(has(ntext, p) for p in rule["require_any"]):
                return game
        return None

    def product_type(self, ntext: str, game: str) -> tuple[str, Optional[float]]:
        for g, phrases, ptype, msrp in PRODUCT_TYPES:
            if g == game and any(has(ntext, p) for p in phrases):
                return ptype, msrp
        return "Sealed product", None

    def match(self, title: str) -> Optional[Match]:
        n = norm(title)
        if any(i in n for i in self.ignore) or any(has(n, x) for x in NON_TCG):
            return None
        for w in self.watches:
            if all(has_token(n, t) for t in w.get("match", [])) and not any(has_token(n, t) for t in w.get("exclude", [])):
                game = w.get("game", "pokemon")
                ptype, generic_msrp = self.product_type(n, game)
                # unnamed set-wide entries (e.g. "30th celebration") also match toys and merch,
                # so they need some sign this is a card product
                if not w.get("name") and ptype == "Sealed product" and not self.detect_game(n) and not has(n, "card"):
                    continue
                return Match(game=game, product_type=w.get("type", ptype), msrp=w.get("msrp", generic_msrp),
                             priority=PRIORITY.get(str(w.get("priority", "high")), 4),
                             watch_id=w.get("id", ""), watch_name=w.get("name", ""),
                             tcgplayer_id=w.get("tcgplayer_id"))
        if not self.generic_enabled:
            return None
        game = self.detect_game(n)
        if not game:
            return None
        ptype, msrp = self.product_type(n, game)
        return Match(game=game, product_type=ptype, msrp=msrp, priority=self.generic_priority)

    def price_ok(self, price: Optional[float], m: Match) -> bool:
        """False when the price looks like a scalper/marketplace markup."""
        if price is None or m.msrp is None:
            return True
        return price <= m.msrp * self.mult + 0.01

    @staticmethod
    def below_msrp(price: Optional[float], m: Match) -> bool:
        return price is not None and m.msrp is not None and price <= m.msrp * 0.95
