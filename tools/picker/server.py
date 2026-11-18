#!/usr/bin/env python3
"""Tal photo picker - a small local server for choosing item photos from Google Images results.

Run:  python tools/picker/server.py      (opens http://127.0.0.1:8765 in the browser)

Image searches go through the Serper API (Google Images) from this server, never from the page.
Chosen photos are written to images/<id>.jpg with images/manifest.json and images/credits.json kept in step.
The Serper key lives in tools/picker/state/serper_key.txt (git-ignored) or the PICKER_SERPER_KEY variable.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageOps


# --------------------------------------------------------------------------------------------- settings
class Config:
    HOST = "127.0.0.1"
    PORT = int(os.environ.get("PICKER_PORT", "8765"))
    OPEN_BROWSER = os.environ.get("PICKER_NO_BROWSER") != "1"

    HERE = Path(__file__).resolve().parent
    ROOT = HERE.parent.parent                      # repo root
    ITEMS_FILE = ROOT / "data" / "items.json"
    IMAGES = ROOT / "images"
    MANIFEST = IMAGES / "manifest.json"
    CREDITS = IMAGES / "credits.json"
    PAGE = HERE / "picker.html"
    STATE_DIR = HERE / "state"                     # local only, git-ignored
    REVIEWED = STATE_DIR / "reviewed.json"
    SERPER_KEY_FILE = STATE_DIR / "serper_key.txt"
    SERPER_USAGE = STATE_DIR / "serper_usage.json"

    # The three forced photos come first. Names match index.html.
    FORCE_ITEMS = [
        {"id": "force-1", "name": "פחי צבע"},
        {"id": "force-2", "name": "קרם הגנה"},
        {"id": "force-3", "name": "דגל ישראל"},
    ]

    # Saved photo
    MAX_SIDE = 1400
    JPEG_QUALITY = 84                              # same as scripts/fetch_images.py

    # Search (Serper = Google Images)
    SERPER_URL = "https://google.serper.dev/images"
    SERPER_GL = "il"                               # country: Israel
    SERPER_HL = "iw"                               # language: Hebrew (Google's code)
    SERPER_NUM = 30                                # results per query: 5 pages of 6 for one query
    PAGE_SIZE = 6
    SKIP_EXT = (".svg",)                           # can't be converted to JPG

    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
    HTTP_TIMEOUT = 15
    MAX_DOWNLOAD = 25 * 1024 * 1024
    IMAGE_CACHE_ENTRIES = 400                      # downloaded candidates kept in memory

    GIT_BRANCH = "main"


# --------------------------------------------------------------------------------------------- http helper
class Http:
    _SAFE = ":/?#[]@!$&'()*+,;=%~"

    @classmethod
    def ascii_url(cls, url: str) -> str:
        """Percent-encode non-ASCII characters (Hebrew paths, IDN hosts) so urllib can send it."""
        parts = urllib.parse.urlsplit(url.strip())
        try:
            host = parts.hostname.encode("idna").decode("ascii") if parts.hostname else ""
        except UnicodeError:
            host = parts.hostname or ""
        netloc = parts.netloc.replace(parts.hostname or "", host, 1) if parts.hostname else parts.netloc
        return urllib.parse.urlunsplit((parts.scheme, netloc,
                                        urllib.parse.quote(parts.path, safe=cls._SAFE),
                                        urllib.parse.quote(parts.query, safe=cls._SAFE),
                                        urllib.parse.quote(parts.fragment, safe=cls._SAFE)))

    @classmethod
    def get(cls, url: str, headers: dict | None = None, limit: int = Config.MAX_DOWNLOAD) -> tuple[bytes, str]:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("only http(s) URLs")
        extra = {k: cls.ascii_url(v) if k == "Referer" else v for k, v in (headers or {}).items()}
        req = urllib.request.Request(cls.ascii_url(url), headers={
            "User-Agent": Config.USER_AGENT, "Accept-Language": "he,en;q=0.8", "Accept": "*/*", **extra})
        with urllib.request.urlopen(req, timeout=Config.HTTP_TIMEOUT) as r:
            data = r.read(limit + 1)
            if len(data) > limit:
                raise ValueError("file too large")
            return data, r.headers.get("Content-Type", "")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_atomic(path: Path, data: bytes | str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


# --------------------------------------------------------------------------------------------- search
class SearchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code                           # "nokey" | "badkey" | "credits" | "failed"


class SerperSearch:
    """Google Images results through serper.dev. One request fetches SERPER_NUM results; "More" pages
    through those, and only asks Serper again after they run out. Counts every request it makes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache: dict[str, dict] = {}           # query -> {"list": [...], "next": serper page, "done": bool}
        self.query_locks: dict[str, threading.Lock] = {}
        self.by_key: dict[str, dict] = {}
        self.usage = read_json(Config.SERPER_USAGE, {"queries": 0, "credits": 0})

    @staticmethod
    def key() -> str:
        env = os.environ.get("PICKER_SERPER_KEY", "").strip()
        if env:
            return env
        try:
            return Config.SERPER_KEY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _count(self, credits: int):
        with self.lock:
            self.usage["queries"] += 1
            self.usage["credits"] += credits
            write_atomic(Config.SERPER_USAGE, json.dumps(self.usage))

    def _request(self, query: str, page: int) -> list[dict]:
        key = self.key()
        if not key:
            raise SearchError("nokey", f"No Serper key. Put it in {Config.SERPER_KEY_FILE}")
        body = json.dumps({"q": query, "gl": Config.SERPER_GL, "hl": Config.SERPER_HL,
                           "num": Config.SERPER_NUM, "page": page}).encode("utf-8")
        req = urllib.request.Request(Config.SERPER_URL, data=body, method="POST", headers={
            "X-API-KEY": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=Config.HTTP_TIMEOUT + 10) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code in (401, 403):
                code = "credits" if "credit" in detail.lower() else "badkey"
                raise SearchError(code, f"Serper refused the request ({e.code}): {detail}") from None
            raise SearchError("failed", f"Serper error {e.code}: {detail}") from None
        except Exception as e:  # noqa: BLE001
            raise SearchError("failed", f"Could not reach Serper: {e}") from None
        self._count(int(data.get("credits") or 0))
        out = []
        for i in data.get("images", []):
            url = i.get("imageUrl") or ""
            if not url.lower().startswith(("http://", "https://")):
                continue
            if urllib.parse.urlsplit(url).path.lower().endswith(Config.SKIP_EXT):
                continue
            c = {"key": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16], "url": url,
                 "page": i.get("link", ""), "title": i.get("title", ""), "domain": i.get("domain", ""),
                 "w": int(i.get("imageWidth") or 0), "h": int(i.get("imageHeight") or 0)}
            out.append(c)
        return out

    def _lock_for(self, query):
        with self.lock:
            return self.query_locks.setdefault(query, threading.Lock())

    def page(self, query: str, page: int) -> dict:
        query = query.strip()
        need = (page + 1) * Config.PAGE_SIZE
        with self._lock_for(query):
            entry = self.cache.setdefault(query, {"list": [], "next": 1, "done": False, "seen": set()})
            while len(entry["list"]) < need and not entry["done"]:
                found = self._request(query, entry["next"])      # SearchError propagates; nothing cached
                entry["next"] += 1
                fresh = [c for c in found if c["url"] not in entry["seen"]]
                for c in fresh:
                    entry["seen"].add(c["url"])
                    self.by_key[c["key"]] = c
                entry["list"] += fresh
                if len(found) < Config.SERPER_NUM // 2:
                    entry["done"] = True
            return {"query": query, "page": page, "results": entry["list"][page * Config.PAGE_SIZE:need],
                    "more": len(entry["list"]) > need or not entry["done"], "usage": self.usage}


# --------------------------------------------------------------------------------------------- images
class ImageFetcher:
    """Downloads candidate images and keeps the bytes in memory, so showing a tile and then choosing it
    costs only one download."""

    def __init__(self):
        self.cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, url: str, referer: str = "") -> tuple[bytes, str]:
        with self.lock:
            if url in self.cache:
                self.cache.move_to_end(url)
                return self.cache[url]
        try:
            data, ctype = Http.get(url, {"Referer": referer} if referer else {})
            Image.open(io.BytesIO(data)).verify()       # make sure it really is an image
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc}: {e}") from None
        with self.lock:
            self.cache[url] = (data, ctype)
            while len(self.cache) > Config.IMAGE_CACHE_ENTRIES:
                self.cache.popitem(last=False)
        return data, ctype

    @staticmethod
    def to_jpeg(data: bytes) -> bytes:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((Config.MAX_SIDE, Config.MAX_SIDE), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=Config.JPEG_QUALITY, optimize=True)
        return out.getvalue()


# --------------------------------------------------------------------------------------------- repo data
class Store:
    def __init__(self):
        raw = json.loads(Config.ITEMS_FILE.read_text(encoding="utf-8"))
        self.items = [dict(i) for i in Config.FORCE_ITEMS] + raw
        for i in self.items:
            i["force"] = i["id"].startswith("force-")
        self.by_id = {i["id"]: i for i in self.items}
        self.lock = threading.Lock()
        self.reviewed: dict[str, str] = read_json(Config.REVIEWED, {})

    @staticmethod
    def has_photo(item_id):
        return (Config.IMAGES / f"{item_id}.jpg").exists()

    def listing(self):
        return [{**i, "photo": self.has_photo(i["id"]), "reviewed": self.reviewed.get(i["id"])}
                for i in self.items]

    def mark(self, item_id, how):
        with self.lock:
            self.reviewed[item_id] = how
            write_atomic(Config.REVIEWED, json.dumps(self.reviewed, ensure_ascii=False, indent=1))

    def save_photo(self, item_id, jpeg: bytes, url: str, page: str):
        item = self.by_id[item_id]
        with self.lock:
            write_atomic(Config.IMAGES / f"{item_id}.jpg", jpeg)
            # manifest: same rule as scripts/fetch_images.py - every images/*.jpg, sorted
            have = sorted(p.stem for p in Config.IMAGES.glob("*.jpg"))
            write_atomic(Config.MANIFEST, json.dumps(have))
            credits = read_json(Config.CREDITS, {})
            credits[item_id] = {"name": item["name"],
                                "file": urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]) or "clipboard",
                                "from": page or url, "url": url}
            write_atomic(Config.CREDITS, json.dumps(credits, ensure_ascii=False, indent=1))


# --------------------------------------------------------------------------------------------- git
class Git:
    @staticmethod
    def run(*args) -> tuple[int, str]:
        p = subprocess.run(["git", *args], cwd=Config.ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout + p.stderr).strip()

    @classmethod
    def push_images(cls) -> dict:
        log = []
        code, branch = cls.run("rev-parse", "--abbrev-ref", "HEAD")
        if branch != Config.GIT_BRANCH:
            return {"ok": False, "log": f"Not on {Config.GIT_BRANCH} (on {branch})."}
        cls.run("add", "--", "images")
        code, _ = cls.run("diff", "--cached", "--quiet", "--", "images")
        if code != 0:
            _, names = cls.run("diff", "--cached", "--name-only", "--", "images")
            n = sum(1 for x in names.splitlines() if x.endswith(".jpg"))
            code, out = cls.run("commit", "-m", f"Picker: update {n} photo(s)", "--", "images")
            log.append(out)
            if code != 0:
                return {"ok": False, "log": "\n".join(log)}
        else:
            log.append("Nothing new to commit in images/.")
        code, out = cls.run("pull", "--rebase", "--autostash", "origin", Config.GIT_BRANCH)
        log.append(out)
        if code != 0:
            return {"ok": False, "log": "\n".join(log)}
        code, out = cls.run("push", "origin", f"HEAD:{Config.GIT_BRANCH}")
        log.append(out)
        return {"ok": code == 0, "log": "\n".join(log)}


# --------------------------------------------------------------------------------------------- web
class App:
    store = Store()
    search = SerperSearch()
    images = ImageFetcher()


class Handler(BaseHTTPRequestHandler):
    server_version = "TalPicker/2"

    def log_message(self, fmt, *args):  # keep the console quiet except for errors
        if args and str(args[1]).startswith(("4", "5")):
            sys.stderr.write("[http] " + fmt % args + "\n")

    def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _item(self, body):
        item_id = body.get("id")
        return item_id if item_id in App.store.by_id else None

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, Config.PAGE.read_bytes(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            if u.path == "/api/items":
                return self._json({"items": App.store.listing(), "pageSize": Config.PAGE_SIZE,
                                   "usage": App.search.usage, "hasKey": bool(App.search.key())})
            if u.path == "/api/search":
                try:
                    return self._json(App.search.page(q.get("q", ""), int(q.get("page", "0"))))
                except SearchError as e:
                    print(f"[search] {e}")
                    return self._json({"error": str(e), "code": e.code, "results": [], "usage": App.search.usage})
            if u.path.startswith("/photo/"):
                item_id = u.path[len("/photo/"):]
                f = Config.IMAGES / f"{item_id}.jpg"
                if item_id not in App.store.by_id or not f.exists():
                    return self._send(404, b"", "text/plain")
                return self._send(200, f.read_bytes(), "image/jpeg", {"Cache-Control": "no-store"})
            if u.path == "/api/img":
                c = App.search.by_key.get(q.get("k", ""))
                if not c:
                    return self._json({"error": "unknown image"}, 404)
                try:
                    data, ctype = App.images.get(c["url"], c["page"])
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": str(e)[:300]}, 502)
                return self._send(200, data, ctype or "image/jpeg", {"Cache-Control": "max-age=3600"})
            return self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        try:
            body = self._body()
            if u.path == "/api/skip":
                item_id = self._item(body)
                if not item_id:
                    return self._json({"error": "unknown id"}, 400)
                if App.store.reviewed.get(item_id) != "chosen":
                    App.store.mark(item_id, "skipped")
                return self._json({"ok": True})
            if u.path == "/api/choose":
                item_id = self._item(body)
                if not item_id:
                    return self._json({"error": "unknown id"}, 400)
                c = App.search.by_key.get(body.get("key", ""))
                url, page = (c["url"], c["page"]) if c else (body.get("url", "").strip(), "")
                if not url:
                    return self._json({"error": "no image"}, 400)
                try:
                    data, _ = App.images.get(url, page)
                    jpeg = ImageFetcher.to_jpeg(data)
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": f"download failed: {e}"[:400]}, 502)
                App.store.save_photo(item_id, jpeg, url, page)
                App.store.mark(item_id, "chosen")
                return self._json({"ok": True, "bytes": len(jpeg)})
            if u.path == "/api/upload":                # an image pasted from the clipboard
                item_id = self._item(body)
                if not item_id:
                    return self._json({"error": "unknown id"}, 400)
                try:
                    jpeg = ImageFetcher.to_jpeg(base64.b64decode(body.get("data", "")))
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": f"not an image: {e}"[:300]}, 400)
                App.store.save_photo(item_id, jpeg, "", body.get("from") or "pasted from clipboard")
                App.store.mark(item_id, "chosen")
                return self._json({"ok": True, "bytes": len(jpeg)})
            if u.path == "/api/push":
                return self._json(Git.push_images())
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 500)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    Config.STATE_DIR.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer((Config.HOST, Config.PORT), Handler)
    url = f"http://{Config.HOST}:{Config.PORT}/"
    print(f"Tal picker running at {url}  (Ctrl+C to stop)")
    if not App.search.key():
        print(f"No Serper key yet: put it in {Config.SERPER_KEY_FILE}")
    if Config.OPEN_BROWSER:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
