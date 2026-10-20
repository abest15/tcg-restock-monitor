"""Community intel: new posts on deal/restock subreddits. Local sightings (posts that
mention your city, a nearby store or area) are pushed with higher priority."""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET

from ..matcher import has, norm
from ..models import CheckResult, Signal
from .base import Source, clean

log = logging.getLogger(__name__)
ATOM = "{http://www.w3.org/2005/Atom}"


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for e in root.iter(f"{ATOM}entry"):
        link = e.find(f"{ATOM}link")
        content = e.find(f"{ATOM}content")
        out.append({
            "id": (e.findtext(f"{ATOM}id") or "").strip(),
            "title": clean(e.findtext(f"{ATOM}title")),
            "url": link.get("href") if link is not None else "",
            "body": clean(content.text if content is not None else "")[:1500],
            "raw": content.text if content is not None else "",
        })
    return out


# stores whose stock is mostly in-store and invisible online; community posts are the best signal
STORE_WORDS = ["dollar general", "dollar tree", "family dollar", "five below", "costco", "ace hardware",
               "sam s club", "sams club", "walgreens", "cvs", "kroger", "king soopers", "meijer", "fred meyer"]

TARGET_LINK = re.compile(r"target\.com/[^\s\"'<>]*?A-(\d{6,10})", re.I)


class RedditSource(Source):
    name = "reddit"
    label = "Reddit"
    # Reddit throttles generic browser user agents; it asks bots to identify themselves
    UA = "windows:tcg-restock-monitor:1.1 (personal restock alerts)"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.primed: set[str] = set()

    def harvest_target_links(self, posts: list[dict]) -> None:
        """Deal posts link Target products constantly; feed those item numbers to the Target monitor."""
        seeds = self.ctx.db.get("target_seed_tcins", {}) or {}
        added = 0
        for p in posts:
            if not self.ctx.matcher.match(p["title"]):
                continue
            for tcin in TARGET_LINK.findall(p.get("raw", "") + " " + p.get("url", "")):
                if tcin not in seeds:
                    seeds[tcin] = p["title"][:150]
                    added += 1
        if added:
            self.ctx.db.set("target_seed_tcins", seeds)
            log.info("reddit: %d new Target items to monitor", added)

    def check(self) -> CheckResult:
        res = CheckResult()
        local = [t for t in self.scfg.get("local_terms", []) if t]
        mode = self.scfg.get("mode", "watchlist")  # watchlist | local_only | all
        subs = self.scfg.get("subreddits", [])
        # one combined feed (r/a+b) = one request, which stays under Reddit's anonymous rate limit
        for sub in (["+".join(subs)] if subs else []):
            try:
                r = self.http.get(f"https://www.reddit.com/r/{sub}/new/.rss?limit=50", check_block=False,
                                  headers={"Accept": "application/atom+xml", "User-Agent": self.UA})
                posts = parse_feed(r.text)
            except Exception as e:  # noqa: BLE001
                log.info("reddit r/%s: %s", sub, e)
                res.complete = False
                continue
            first = sub not in self.primed
            self.primed.add(sub)
            self.harvest_target_links(posts)
            for p in posts:
                text = norm(p["title"] + " " + p["body"])
                is_local = any(has(text, t) for t in local)
                store = next((s for s in STORE_WORDS if has(text, s)), "")
                m = self.ctx.matcher.match(p["title"]) or self.ctx.matcher.match(p["title"] + " " + p["body"][:300])
                watched = bool(m and m.watch_id)
                if mode == "local_only" and not is_local:
                    continue
                if mode == "watchlist" and not (is_local and m) and not watched and not (store and m):
                    continue
                if mode == "all" and not m:
                    continue
                res.signals.append(Signal(
                    source=self.name, key=f"reddit:{p['id']}",
                    title=("📍 Local sighting: " if is_local else (f"{store.title()} find: " if store else "Reddit: "))
                          + p["title"][:120],
                    message=(p["body"][:300] or p["title"]),
                    url=p["url"], priority=4 if is_local else 3,
                    tags=["round_pushpin" if is_local else "speech_balloon"],
                    game=m.game if m else "", quiet=first))
        return res
