from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

IN_STOCK = "IN_STOCK"
LIMITED = "LIMITED"          # in stock but low quantity
PREORDER = "PREORDER"        # pre-order is open and buyable
COMING_SOON = "COMING_SOON"  # listed but not buyable yet
OUT_OF_STOCK = "OUT_OF_STOCK"
UNKNOWN = "UNKNOWN"

BUYABLE = {IN_STOCK, LIMITED, PREORDER}


def is_buyable(status: Optional[str]) -> bool:
    return status in BUYABLE


@dataclass
class StoreStock:
    """Stock of one product at one physical store."""
    store_id: str
    name: str
    status: str = UNKNOWN
    quantity: Optional[int] = None
    distance_miles: Optional[float] = None
    address: str = ""
    pickup: Optional[bool] = None   # can be ordered online for same-day pickup


@dataclass
class Listing:
    """One product as seen at one retailer on one check."""
    retailer: str
    sku: str
    title: str
    url: str
    status: str = UNKNOWN
    price: Optional[float] = None
    seller: str = ""                # empty / retailer name = sold by the retailer itself
    third_party: bool = False
    image: str = ""
    release_date: str = ""
    stores: list[StoreStock] = field(default_factory=list)
    stores_checked: bool = False    # True when `stores` is a complete nearby-store snapshot
    new_discovery: bool = True      # False = known since startup; its current stock is not news
    trusted_price: bool = False     # retailer has no marketplace scalpers; skip the MSRP price filter

    @property
    def key(self) -> str:
        return f"{self.retailer}:{self.sku}"


@dataclass
class Signal:
    """A non-inventory event worth alerting on (queue up, community sighting...)."""
    source: str
    key: str            # dedupe key
    title: str
    message: str
    url: str = ""
    priority: int = 3   # ntfy priority 1..5
    tags: list[str] = field(default_factory=list)
    game: str = ""
    quiet: bool = False  # record as seen without notifying (first fetch of a feed)


@dataclass
class CheckResult:
    listings: list[Listing] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    complete: bool = True   # False if the source was partially blocked; avoids false "sold out"
