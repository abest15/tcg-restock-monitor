"""Self-update from GitHub. A few times a day the bot compares its version with the latest
published one; if newer, it downloads it, replaces its own program files, installs any new
requirements, tells your phone, and restarts (start.bat / the service bring it back up).

Never touched: config.yaml, data/, portfolio.yaml, target_items.txt, .venv/.
watchlist.yaml: replaced only if you haven't edited it; otherwise the new one is saved as
watchlist.new.yaml so your edits are kept.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

from . import __version__
from .config import DATA_DIR, ROOT

log = logging.getLogger(__name__)

PROTECTED = {"config.yaml", "keys.yaml", "data", "portfolio.yaml", "target_items.txt", ".venv", ".git"}
USER_EDITABLE = {"watchlist.yaml"}
RESTART_EXIT_CODE = 3


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


class Updater:
    def __init__(self, cfg: dict, http, db, notifier):
        u = cfg.get("updates", {}) or {}
        self.enabled = u.get("enabled", True)
        self.repo = u.get("repo", "")
        self.branch = u.get("branch", "main")
        self.every = float(u.get("check_hours", 6)) * 3600
        self.http = http
        self.db = db
        self.n = notifier

    def remote_version(self) -> Optional[str]:
        url = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/restockbot/__init__.py"
        r = self.http.get(url, params={"t": int(time.time())}, check_block=False)
        if r.status_code != 200:
            raise RuntimeError(f"version check got HTTP {r.status_code}")
        m = re.search(r'__version__\s*=\s*"([^"]+)"', r.text)
        return m.group(1) if m else None

    def due(self) -> bool:
        return self.enabled and bool(self.repo) and time.time() - (self.db.get("update_checked", 0) or 0) > self.every

    def check_and_apply(self, force: bool = False) -> bool:
        """Returns True when an update was installed (caller should restart)."""
        if not self.enabled or not self.repo:
            return False
        self.db.set("update_checked", time.time())
        try:
            latest = self.remote_version()
        except Exception as e:  # noqa: BLE001
            log.info("update check failed: %s", e)
            return False
        if not latest or (parse_version(latest) <= parse_version(__version__) and not force):
            log.info("bot is up to date (v%s)", __version__)
            return False
        log.warning("updating v%s -> v%s", __version__, latest)
        try:
            notes = self.install()
        except Exception as e:  # noqa: BLE001
            log.exception("update failed; still running v%s", __version__)
            self.n.send("⚠️ Bot update failed", f"Couldn't install v{latest}: {str(e)[:200]}\n"
                        f"Still running v{__version__}. It will try again later.", priority=3, tags=["warning"])
            return False
        self.n.send(f"🔄 Restock bot updated to v{latest}", (notes or "Improvements and fixes.")[:1500]
                    + "\nRestarting now. Nothing for you to do.", priority=2, tags=["arrows_counterclockwise"])
        return True

    def install(self) -> str:
        url = f"https://codeload.github.com/{self.repo}/zip/refs/heads/{self.branch}"
        r = self.http.get(url, check_block=False)
        if r.status_code != 200:
            raise RuntimeError(f"download got HTTP {r.status_code}")
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        top = names[0].split("/")[0] + "/"
        if f"{top}restockbot/__main__.py" not in names:
            raise RuntimeError("downloaded package looks wrong (no restockbot/__main__.py)")
        stage = DATA_DIR / "update_stage"
        backup = DATA_DIR / f"backup_v{__version__}"
        shutil.rmtree(stage, ignore_errors=True)
        z.extractall(stage)
        src_root = stage / top.rstrip("/")
        old_req = sha(ROOT / "requirements.txt")

        # back up current program files, then copy the new ones in
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True)
        notes = []
        for item in src_root.iterdir():
            if item.name in PROTECTED:
                continue
            dest = ROOT / item.name
            if dest.exists():
                (shutil.copytree if dest.is_dir() else shutil.copy2)(dest, backup / item.name)
            if item.name in USER_EDITABLE and dest.exists() and sha(dest) != (self.db.get(f"shipped:{item.name}") or sha(dest)):
                shutil.copy2(item, ROOT / (item.stem + ".new" + item.suffix))
                notes.append(f"You've edited {item.name}, so it was kept. The new version is {item.stem}.new{item.suffix}.")
                continue
            if item.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
            if item.name in USER_EDITABLE:
                self.db.set(f"shipped:{item.name}", sha(dest))

        # new code must at least import; otherwise roll back
        check = subprocess.run([sys.executable, "-c", "import restockbot.__main__"], cwd=ROOT,
                               capture_output=True, text=True, timeout=120)
        if check.returncode != 0:
            for item in backup.iterdir():
                dest = ROOT / item.name
                if dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
            raise RuntimeError(f"new version failed to load, rolled back: {check.stderr[-300:]}")

        if sha(ROOT / "requirements.txt") != old_req:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--only-binary=:all:", "-r",
                            str(ROOT / "requirements.txt")], cwd=ROOT, timeout=600)
        shutil.rmtree(stage, ignore_errors=True)

        changelog = ROOT / "CHANGELOG.md"
        if changelog.exists():
            first = changelog.read_text(encoding="utf-8").split("\n## ")
            if len(first) > 1:
                notes.insert(0, "## " + first[1].strip())
        return "\n".join(notes)

    def remember_watchlist(self) -> None:
        """On first run, record the shipped watchlist so later updates can tell if you edited it."""
        if not self.db.get("shipped:watchlist.yaml"):
            self.db.set("shipped:watchlist.yaml", sha(ROOT / "watchlist.yaml"))
