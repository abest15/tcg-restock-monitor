from __future__ import annotations

import logging
import random
import threading
import time
from typing import Optional

import requests

try:  # connects like a real Chrome browser; retailers' bot protection flags plain Python connections
    from curl_cffi import requests as browser_requests
except ImportError:  # pragma: no cover
    browser_requests = None

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

BLOCK_MARKERS = (
    "robot or human",
    "are you a robot",
    "captcha",
    "access denied",
    "incapsula",
    "px-captcha",
    "request unsuccessful",
    "pardon our interruption",
)


class Blocked(Exception):
    """The retailer's bot protection refused the request."""


class Http:
    """Shared requests session with browser-like headers, retries and per-host backoff."""

    def __init__(self, timeout: float = 20, user_agent: str = ""):
        self.timeout = timeout
        self.user_agent = user_agent
        self.browser = browser_requests is not None
        if not self.browser:
            log.warning("curl_cffi not installed: using plain Python connections, which stores block more often. "
                        "Fix: .venv\\Scripts\\python -m pip install curl_cffi")
        # Stores are checked in parallel threads. curl_cffi sessions aren't safe to share between threads,
        # so each thread gets its own session (cookies stay per-thread, which is fine: one source = one thread).
        self._local = threading.local()
        self._backoff_until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}

    @property
    def s(self):
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = self._new_session()
            self._local.session = sess
        return sess

    def _new_session(self):
        if self.browser:
            sess = browser_requests.Session(impersonate="chrome")  # matching Chrome User-Agent and headers
            sess.headers.update({"Accept-Language": "en-US,en;q=0.9"})
            if self.user_agent:
                sess.headers["User-Agent"] = self.user_agent
            return sess
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": self.user_agent or DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        })
        return sess

    def _host(self, url: str) -> str:
        return url.split("/")[2] if "://" in url else url

    def backing_off(self, url: str) -> bool:
        return time.time() < self._backoff_until.get(self._host(url), 0)

    def _penalize(self, host: str) -> None:
        strikes = self._strikes.get(host, 0) + 1
        self._strikes[host] = strikes
        wait = min(60 * 2 ** (strikes - 1), 3600) * random.uniform(0.8, 1.2)
        self._backoff_until[host] = time.time() + wait
        log.warning("%s is blocking/limiting us; backing off %.0f min", host, wait / 60)

    def get(self, url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None,
            allow_redirects: bool = True, check_block: bool = True) -> requests.Response:
        host = self._host(url)
        if self.backing_off(url):
            raise Blocked(f"{host} (cooling down after being blocked)")
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = self.s.get(url, params=params, headers=headers, timeout=self.timeout,
                               allow_redirects=allow_redirects)
            except Exception as e:  # noqa: BLE001  (requests and curl_cffi raise different types)
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (403, 429, 435) or (
                check_block and r.status_code == 200 and "text/html" in r.headers.get("content-type", "")
                and len(r.text) < 20000 and any(m in r.text.lower() for m in BLOCK_MARKERS)
            ):
                self._penalize(host)
                raise Blocked(f"{host} returned {r.status_code}")
            if r.status_code >= 500:
                last_exc = requests.HTTPError(f"{r.status_code} from {host}")
                time.sleep(2 * (attempt + 1))
                continue
            self._strikes[host] = 0
            return r
        raise last_exc or requests.RequestException(f"failed to fetch {url}")

    def json(self, url: str, **kw) -> dict:
        r = self.get(url, **kw)
        if r.status_code >= 400:
            # include the body: APIs usually say which parameter they didn't like
            raise requests.HTTPError(f"{r.status_code} from {self._host(url)}: {r.text[:300]}", response=r)
        return r.json()

    def post(self, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        return self.s.post(url, **kw)
