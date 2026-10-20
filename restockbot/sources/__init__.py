from __future__ import annotations

from ..http import Http
from .base import Context, Source
from .bestbuy import BestBuySource
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
    "pokemoncenter": PokemonCenterSource,
    "shopify": ShopifySource,
    "pagewatch": PageWatchSource,
    "sitesearch": SiteSearchSource,
    "reddit": RedditSource,
    "tcgplayer": TCGplayerSource,
    "sets": SetsSource,
}


def build_sources(cfg: dict, watchlist: dict, http: Http, ctx: Context, only: list[str] | None = None) -> list[Source]:
    out: list[Source] = []
    for name, cls in REGISTRY.items():
        scfg = cfg["sources"].get(name, {})
        if only is not None:
            if name not in only:
                continue
        elif not scfg.get("enabled") and not (name == "shopify" and watchlist.get("shops")):
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
