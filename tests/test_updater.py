import io
import zipfile

from restockbot import updater


class R:
    def __init__(self, status, text="", content=b""):
        self.status_code, self.text, self.content = status, text, content


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(f"tcg-restock-monitor-main/{name}", body)
    return buf.getvalue()


class FakeDB(dict):
    def get(self, k, d=None):
        return super().get(k, d)

    def set(self, k, v):
        self[k] = v


class FakeN:
    def __init__(self):
        self.sent = []

    def send(self, title, msg, **kw):
        self.sent.append(title)


def setup_root(tmp_path, monkeypatch):
    root = tmp_path / "bot"
    (root / "restockbot").mkdir(parents=True)
    (root / "restockbot" / "__init__.py").write_text('__version__ = "2.0.0"\n')
    (root / "restockbot" / "__main__.py").write_text("x = 1\n")
    (root / "config.yaml").write_text("ntfy: {topic: mine}\n")
    (root / "watchlist.yaml").write_text("products: []\n")
    (root / "requirements.txt").write_text("requests\n")
    monkeypatch.setattr(updater, "ROOT", root)
    monkeypatch.setattr(updater, "DATA_DIR", root / "data")
    return root


def test_update_installs_and_protects_user_files(tmp_path, monkeypatch):
    root = setup_root(tmp_path, monkeypatch)
    z = make_zip({"restockbot/__init__.py": '__version__ = "2.1.0"\n', "restockbot/__main__.py": "x = 2\n",
                  "config.yaml": "ntfy: {topic: SHOULD-NOT-COPY}\n", "watchlist.yaml": "products: [new]\n",
                  "requirements.txt": "requests\n", "CHANGELOG.md": "# Changes\n\n## 2.1.0\n- Added Foil Pack Fanatic\n"})

    class Http:
        def get(self, url, **kw):
            return R(200, text='__version__ = "2.1.0"') if "raw.githubusercontent" in url else R(200, content=z)

    db, n = FakeDB(), FakeN()
    u = updater.Updater({"updates": {"repo": "abest15/tcg-restock-monitor"}}, Http(), db, n)
    u.remember_watchlist()
    assert u.check_and_apply()
    assert (root / "restockbot/__main__.py").read_text() == "x = 2\n"
    assert "mine" in (root / "config.yaml").read_text()             # never overwritten
    assert (root / "watchlist.yaml").read_text() == "products: [new]\n"  # unedited -> replaced
    assert "v2.1.0" in n.sent[-1]


def test_edited_watchlist_is_kept_and_bad_update_rolls_back(tmp_path, monkeypatch):
    root = setup_root(tmp_path, monkeypatch)
    db, n = FakeDB(), FakeN()
    u = updater.Updater({"updates": {"repo": "x/y"}}, None, db, n)
    u.remember_watchlist()
    (root / "watchlist.yaml").write_text("products: [my edits]\n")
    z = make_zip({"restockbot/__init__.py": '__version__ = "2.1.0"\n', "restockbot/__main__.py": "raise SystemExit(1)\n",
                  "watchlist.yaml": "products: [new]\n"})

    class Http:
        def get(self, url, **kw):
            return R(200, text='__version__ = "2.1.0"') if "raw.githubusercontent" in url else R(200, content=z)

    u.http = Http()
    assert not u.check_and_apply()                                   # new code fails to load
    assert (root / "restockbot/__main__.py").read_text() == "x = 1\n"  # rolled back
    assert "failed" in n.sent[-1]


def test_no_update_when_current(tmp_path, monkeypatch):
    setup_root(tmp_path, monkeypatch)

    class Http:
        def get(self, url, **kw):
            return R(200, text=f'__version__ = "{updater.__version__}"')

    assert not updater.Updater({"updates": {"repo": "x/y"}}, Http(), FakeDB(), FakeN()).check_and_apply()
