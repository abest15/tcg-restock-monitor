from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import Optional

import requests

log = logging.getLogger(__name__)


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    a, b, t = _parse_hhmm(start), _parse_hhmm(end), now.time()
    return (a <= t < b) if a <= b else (t >= a or t < b)


class Notifier:
    """Sends push notifications to the ntfy app (https://ntfy.sh)."""

    def __init__(self, cfg: dict, dry_run: bool = False):
        n = cfg["ntfy"]
        self.server = n["server"].rstrip("/")
        self.topic = n["topic"]
        self.token = n.get("token") or ""
        self.quiet = cfg["alerts"]["quiet_hours"]
        self.dry_run = dry_run

    def send(self, title: str, message: str, *, priority: int = 3, tags: Optional[list[str]] = None,
             click: str = "", actions: Optional[list[dict]] = None, image: str = "") -> bool:
        if self.quiet.get("enabled") and priority < int(self.quiet.get("min_priority", 5)):
            if in_quiet_hours(datetime.now(), self.quiet["start"], self.quiet["end"]):
                log.info("[quiet hours] suppressed: %s", title)
                return False
        payload: dict = {
            "topic": self.topic,
            "title": title[:250],
            "message": message[:3800],
            "priority": max(1, min(5, int(priority))),
            "tags": tags or [],
        }
        if click:
            payload["click"] = click
        if actions:
            payload["actions"] = actions[:3]
        if image:
            payload["attach"] = image
        log.info("ALERT p%s | %s | %s", payload["priority"], title, message.replace("\n", " / ")[:200])
        if self.dry_run or not self.topic:
            return True
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = requests.post(self.server, json=payload, headers=headers, timeout=15)
            if r.status_code >= 400:
                log.error("ntfy error %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as e:
            log.error("ntfy send failed: %s", e)
            return False


def view_action(label: str, url: str) -> dict:
    return {"action": "view", "label": label, "url": url, "clear": False}


def maps_url(query: str) -> str:
    from urllib.parse import quote_plus
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"
