from __future__ import annotations

from ..http import Http
from .base import Context, Source
from .bestbuy import BestBuySource
from .kroger import KrogerSource
from .pagewatch import PageWatchSource
from .pokemoncenter import PokemonCenterSource
from .reddit import RedditSource
from .shopify import ShopifySource
from .sitesearch import SiteSearchSource
from .target import TargetSource
from .sets import SetsSource, SetTracker
from .tcgplayer import TCGplayerSource
from .walmart import WalmartSource

REGISTRY = {
    "target": TargetSource,
    "bestbuy": BestBuySource,
    "walmart": WalmartSource,
    "kroger": KrogerSource,
    "pokemoncenter": PokemonCenterSource,
    "shopify": ShopifySource,
    "pagewatch": PageWatchSource,
    "sitesearch": SiteSearchSource,
    "reddit": RedditSource,
    "tcgplayer": TCGplayerSource,
    "sets": SetsSource,
}


def is_enabled(name: str, scfg: dict, watchlist: dict) -> bool:
    if name == "shopify" and watchlist.get("shops"):
        return True
    if name == "kroger":  # needs free API keys; stays off (and quiet) until they're added
        return bool(scfg.get("enabled") and scfg.get("client_id") and scfg.get("client_secret"))
    return bool(scfg.get("enabled"))


def build_sources(cfg: dict, watchlist: dict, http: Http, ctx: Context, only: list[str] | None = None) -> list[Source]:
    out: list[Source] = []
    for name, cls in REGISTRY.items():
        scfg = cfg["sources"].get(name, {})
        if only is not None:
            if name not in only:
                continue
        elif not is_enabled(name, scfg, watchlist):
            continue
        if cls is SetsSource:
            out.append(cls(scfg, http, ctx, tracker=ctx.tracker))
        elif cls is ShopifySource:
            out.append(cls(scfg, http, ctx, shops=watchlist.get("shops") or []))
        elif cls is TCGplayerSource:
            out.append(cls(scfg, http, ctx, watchlist=watchlist))
        else:
            out.append(cls(scfg, http, ctx))
    return out
