#!/usr/bin/env python3
"""Tal photo picker - a small local server for reviewing and replacing item photos.

Run:  python tools/picker/server.py      (opens http://127.0.0.1:8765 in the browser)

Everything runs on this PC. Image searches are made from here (not from the page), and chosen photos
are written to images/<id>.jpg with images/manifest.json and images/credits.json kept in step.
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
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
    MISSING = IMAGES / "missing.txt"
    PAGE = HERE / "picker.html"
    STATE_DIR = HERE / "state"                     # local only, gitignored
    REVIEWED = STATE_DIR / "reviewed.json"

    # The three forced photos come first. Names match index.html; "en" feeds the English search button.
    FORCE_ITEMS = [
        {"id": "force-1", "name": "פחי צבע", "en": "open paint cans colors"},
        {"id": "force-2", "name": "קרם הגנה", "en": "sunscreen bottle"},
        {"id": "force-3", "name": "דגל ישראל", "en": "Israeli flag"},
    ]

    # Saved photo
    MAX_SIDE = 1400
    JPEG_QUALITY = 84                              # same as scripts/fetch_images.py

    # Search
    PAGE_SIZE = 6
    THIN_BATCH = 0.25                              # a batch under 25% full -> also ask the next source
    MIN_WIDTH = 800                                # preferred: landscape and at least this wide
    BAD_EXT = (".svg", ".gif", ".ico")
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
    API_USER_AGENT = "TalPicker/1.0 (personal photo picker; python-urllib)"   # Wikimedia asks for this
    API_UA_HOSTS = ("wikimedia.org", "wikipedia.org")
    HTTP_TIMEOUT = 15
    MAX_DOWNLOAD = 25 * 1024 * 1024

    # Optional Google Custom Search (plugged in first when both are set)
    GOOGLE_KEY = os.environ.get("PICKER_GOOGLE_KEY", "")
    GOOGLE_CX = os.environ.get("PICKER_GOOGLE_CX", "")

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
    def get(cls, url: str, headers: dict | None = None, limit: int = Config.MAX_DOWNLOAD) -> tuple[int, bytes, str]:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("only http(s) URLs")
        extra = {k: cls.ascii_url(v) if k == "Referer" else v for k, v in (headers or {}).items()}
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        ua = Config.API_USER_AGENT if host.endswith(Config.API_UA_HOSTS) else Config.USER_AGENT
        req = urllib.request.Request(cls.ascii_url(url), headers={
            "User-Agent": ua,
            "Accept-Language": "he,en;q=0.8",
            "Accept": "*/*",
            **extra,
        })
        with urllib.request.urlopen(req, timeout=Config.HTTP_TIMEOUT) as r:
            data = r.read(limit + 1)
            if len(data) > limit:
                raise ValueError("file too large")
            return r.status, data, r.headers.get("Content-Type", "")


# --------------------------------------------------------------------------------------------- search
class Candidate(dict):
    """{key, url, alt (fallback copy), page, title, w, h}"""

    @classmethod
    def make(cls, url, page="", title="", w=0, h=0, alt=""):
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return cls(key=key, url=url, alt=alt, page=page, title=title, w=int(w or 0), h=int(h or 0))


class Blocked(RuntimeError):
    """The provider refuses scripted requests (bot challenge); skip it for the rest of the session."""


class SearchProvider:
    name = "base"
    batch = 35
    min_interval = 0.0                               # seconds between requests to this provider

    def __init__(self):
        self._lock = threading.Lock()
        self._last = 0.0

    def throttle(self):
        with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def fetch(self, query: str, offset: int) -> list[Candidate]:
        raise NotImplementedError


class DuckDuckGoSearch(SearchProvider):
    """No key needed, but DuckDuckGo often answers scripts with a bot challenge; then we fall back."""
    name = "duckduckgo"
    batch = 100

    def __init__(self):
        super().__init__()
        self._next: dict[str, str] = {}

    def fetch(self, query, offset):
        if offset and query not in self._next:
            return []
        self.throttle()
        page = Http.get("https://duckduckgo.com/?" + urllib.parse.urlencode(
            {"q": query, "iax": "images", "ia": "images"}))[1].decode("utf-8", "replace")
        m = re.search(r'vqd=["\']?([\d-]+)', page)
        if not m:
            raise RuntimeError("no vqd token")
        params = {"l": "wt-wt", "o": "json", "q": query, "vqd": m.group(1),
                  "f": ",size:Large,,layout:Wide,", "p": "1"}
        url = "https://duckduckgo.com/i.js?" + urllib.parse.urlencode(params)
        if offset:
            url = "https://duckduckgo.com/" + self._next[query]
        status, body, _ = Http.get(url, {"Referer": "https://duckduckgo.com/"})
        data = json.loads(body.decode("utf-8", "replace"))
        if status != 200 or "results" not in data:
            raise Blocked("bot challenge")
        if data.get("next"):
            self._next[query] = data["next"]
        else:
            self._next.pop(query, None)
        return [Candidate.make(r["image"], r.get("url", ""), r.get("title", ""), r.get("width"),
                               r.get("height"), alt=r.get("thumbnail", "")) for r in data["results"]]


class BingSearch(SearchProvider):
    name = "bing"
    batch = 35
    min_interval = 1.5
    _anchor = re.compile(r'<a[^>]+class="iusc"[^>]*>', re.S)

    def fetch(self, query, offset):
        self.throttle()
        url = "https://www.bing.com/images/async?" + urllib.parse.urlencode({
            "q": query, "first": offset, "count": self.batch, "mmasync": 1,
            "qft": "+filterui:imagesize-large+filterui:photo-photo"})
        page = Http.get(url)[1].decode("utf-8", "replace")
        out = []
        for tag in self._anchor.findall(page):
            m = re.search(r'\sm="([^"]+)"', tag)
            if not m:
                continue
            try:
                meta = json.loads(html.unescape(m.group(1)))
            except ValueError:
                continue
            href = html.unescape(re.search(r'href="([^"]*)"', tag).group(1)) if 'href="' in tag else ""
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
            w = (qs.get("expw") or ["0"])[0]
            h = (qs.get("exph") or ["0"])[0]
            alt = (qs.get("cdnurl") or [""])[0]
            if meta.get("murl"):
                out.append(Candidate.make(meta["murl"], meta.get("purl", ""), meta.get("t", ""), w, h, alt))
        return out


class GoogleSearch(SearchProvider):
    """Google Custom Search JSON API. Active only when PICKER_GOOGLE_KEY and PICKER_GOOGLE_CX are set."""
    name = "google"
    batch = 10

    def fetch(self, query, offset):
        if offset >= 100:  # API limit
            return []
        url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode({
            "key": Config.GOOGLE_KEY, "cx": Config.GOOGLE_CX, "q": query, "searchType": "image",
            "imgType": "photo", "imgSize": "large", "num": self.batch, "start": offset + 1})
        data = json.loads(Http.get(url)[1])
        return [Candidate.make(i["link"], i.get("image", {}).get("contextLink", ""), i.get("title", ""),
                               i.get("image", {}).get("width"), i.get("image", {}).get("height"),
                               alt=i.get("image", {}).get("thumbnailLink", "")) for i in data.get("items", [])]


class CommonsSearch(SearchProvider):
    """Wikimedia Commons file search (official API, no key). Last resort: few brand photos, but always answers."""
    name = "commons"
    batch = 30
    API = "https://commons.wikimedia.org/w/api.php?"
    THUMB = 1920                                     # a standard Wikimedia thumbnail size (others are refused)

    def fetch(self, query, offset):
        url = self.API + urllib.parse.urlencode({
            "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
            "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": self.batch, "gsroffset": offset,
            "prop": "imageinfo", "iiprop": "url|size|mime", "iiurlwidth": self.THUMB})
        data = json.loads(Http.get(url)[1])
        pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
        out = []
        for p in pages:
            ii = (p.get("imageinfo") or [{}])[0]
            if not ii.get("url") or ii.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
                continue
            out.append(Candidate.make(ii.get("thumburl") or ii["url"], ii.get("descriptionurl", ""),
                                      p.get("title", ""), ii.get("width"), ii.get("height"), alt=ii["url"]))
        return out


class SearchService:
    """Tries providers in order. A provider that answers with a bot challenge is skipped for the rest of
    the session; one that finds nothing (or errors) for a query hands that query to the next provider.
    Keeps every query's ranked results so "More" just pages further."""

    def __init__(self):
        self.providers: list[SearchProvider] = []
        if Config.GOOGLE_KEY and Config.GOOGLE_CX:
            self.providers.append(GoogleSearch())
        self.providers += [DuckDuckGoSearch(), BingSearch(), CommonsSearch()]
        self.disabled: set[str] = set()
        self.cache: dict[str, dict] = {}            # query -> {provider, offset, seen, list, done}
        self.by_key: dict[str, Candidate] = {}
        self.lock = threading.Lock()
        self.query_locks: dict[str, threading.Lock] = {}

    @staticmethod
    def _rank(c: Candidate) -> int:
        path = urllib.parse.urlsplit(c["url"]).path.lower()
        score = 0
        if c["w"] and c["h"]:
            if c["w"] > c["h"]:
                score += 2
            if c["w"] >= Config.MIN_WIDTH:
                score += 2
        if path.endswith((".jpg", ".jpeg")):
            score += 1                               # PNGs are more often logos and cut-outs
        return -score

    def _lock_for(self, query):
        with self.lock:
            return self.query_locks.setdefault(query, threading.Lock())

    def _pull(self, query, entry):
        """Fetch one more batch for this query from the current provider. A provider that errors, is
        blocked, or returns a thin batch (Bing sometimes hides most results from scripts) hands the
        query on to the next provider, whose results are appended."""
        while entry["pi"] < len(self.providers):
            prov = self.providers[entry["pi"]]
            if prov.name in self.disabled:
                entry["pi"], entry["offset"] = entry["pi"] + 1, 0
                continue
            try:
                found = prov.fetch(query, entry["offset"])
            except Blocked as e:
                print(f"[search] {prov.name} blocked ({e}); using the next source from now on")
                self.disabled.add(prov.name)
                found = []
            except Exception as e:  # noqa: BLE001
                print(f"[search] {prov.name} failed for {query!r}: {e}")
                found = []
            fresh = [c for c in found if c["url"] not in entry["seen"]
                     and not urllib.parse.urlsplit(c["url"]).path.lower().endswith(Config.BAD_EXT)]
            for c in fresh:
                entry["seen"].add(c["url"])
                self.by_key[c["key"]] = c
            fresh.sort(key=self._rank)
            entry["list"] += fresh
            if fresh and prov.name not in entry["used"]:
                entry["used"].append(prov.name)
            if len(found) < prov.batch * Config.THIN_BATCH:
                if prov.name not in self.disabled:
                    print(f"[search] {prov.name}: {len(found)} results for {query!r}, adding the next source")
                entry["pi"], entry["offset"] = entry["pi"] + 1, 0
            else:
                entry["offset"] += prov.batch
            return

    def page(self, query: str, page: int) -> dict:
        query = query.strip()
        need = (page + 1) * Config.PAGE_SIZE
        with self._lock_for(query):
            entry = self.cache.setdefault(query, {"pi": 0, "offset": 0, "seen": set(), "list": [], "used": []})
            while len(entry["list"]) < need and entry["pi"] < len(self.providers):
                self._pull(query, entry)
            items = entry["list"][page * Config.PAGE_SIZE:need]
            return {"query": query, "page": page, "provider": " + ".join(entry["used"]),
                    "results": items, "more": len(entry["list"]) > need or entry["pi"] < len(self.providers)}


# --------------------------------------------------------------------------------------------- images
class ImageFetcher:
    """Downloads candidate images (with fallback to the search engine's cached copy) and keeps the
    bytes in memory, so showing a tile and then choosing it costs only one download."""

    def __init__(self):
        self.cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, url: str, alt: str = "", referer: str = "") -> tuple[bytes, str]:
        with self.lock:
            if url in self.cache:
                self.cache.move_to_end(url)
                return self.cache[url]
        errors = []
        for u in [x for x in (url, alt) if x]:
            try:
                headers = {"Referer": referer} if referer else {}
                _, data, ctype = Http.get(u, headers)
                Image.open(io.BytesIO(data)).verify()   # make sure it really is an image
                with self.lock:
                    self.cache[url] = (data, ctype)
                    while len(self.cache) > Config.IMAGE_CACHE_ENTRIES:
                        self.cache.popitem(last=False)
                return data, ctype
            except Exception as e:  # noqa: BLE001
                errors.append(f"{urllib.parse.urlsplit(u).netloc}: {e}")
        raise RuntimeError("; ".join(errors) or "no URL")

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
        Config.STATE_DIR.mkdir(exist_ok=True)
        self.reviewed: dict[str, str] = self._read_json(Config.REVIEWED, {})

    @staticmethod
    def _read_json(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def _write(path: Path, data: bytes | str):
        tmp = path.with_suffix(path.suffix + ".tmp")
        if isinstance(data, str):
            tmp.write_text(data, encoding="utf-8")
        else:
            tmp.write_bytes(data)
        os.replace(tmp, path)

    def has_photo(self, item_id):
        return (Config.IMAGES / f"{item_id}.jpg").exists()

    def listing(self):
        return [{**i, "photo": self.has_photo(i["id"]), "reviewed": self.reviewed.get(i["id"])}
                for i in self.items]

    def mark(self, item_id, how):
        with self.lock:
            self.reviewed[item_id] = how
            self._write(Config.REVIEWED, json.dumps(self.reviewed, ensure_ascii=False, indent=1))

    def save_photo(self, item_id, jpeg: bytes, url: str, page: str):
        item = self.by_id[item_id]
        with self.lock:
            self._write(Config.IMAGES / f"{item_id}.jpg", jpeg)
            # manifest: same rule as scripts/fetch_images.py - every images/*.jpg, sorted
            have = sorted(p.stem for p in Config.IMAGES.glob("*.jpg"))
            self._write(Config.MANIFEST, json.dumps(have))
            credits = self._read_json(Config.CREDITS, {})
            credits[item_id] = {"name": item["name"],
                                "file": urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]) or "clipboard",
                                "from": page or url, "url": url}
            self._write(Config.CREDITS, json.dumps(credits, ensure_ascii=False, indent=1))
            if Config.MISSING.exists():
                lines = Config.MISSING.read_text(encoding="utf-8").splitlines()
                keep = [ln for ln in lines if ln.strip() and not ln.startswith(item_id + "\t")]
                if len(keep) != len([ln for ln in lines if ln.strip()]):
                    self._write(Config.MISSING, "\n".join(keep) + ("\n" if keep else ""))


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
    search = SearchService()
    images = ImageFetcher()


class Handler(BaseHTTPRequestHandler):
    server_version = "TalPicker/1"

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

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, Config.PAGE.read_bytes(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            if u.path == "/api/items":
                return self._json({"items": App.store.listing(), "pageSize": Config.PAGE_SIZE})
            if u.path == "/api/search":
                return self._json(App.search.page(q.get("q", ""), int(q.get("page", "0"))))
            if u.path.startswith("/photo/"):
                item_id = u.path[len("/photo/"):]
                f = Config.IMAGES / f"{item_id}.jpg"
                if item_id not in App.store.by_id or not f.exists():
                    return self._send(404, b"", "text/plain")
                return self._send(200, f.read_bytes(), "image/jpeg", {"Cache-Control": "no-store"})
            if u.path == "/api/img":
                c = App.search.by_key.get(q.get("k", ""))
                url, alt, page = (c["url"], c["alt"], c["page"]) if c else (q.get("u", ""), "", "")
                try:
                    data, ctype = App.images.get(url, alt, page)
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
            if u.path == "/api/keep":
                if body.get("id") not in App.store.by_id:
                    return self._json({"error": "unknown id"}, 400)
                App.store.mark(body["id"], "kept")
                return self._json({"ok": True})
            if u.path == "/api/choose":
                item_id = body.get("id")
                if item_id not in App.store.by_id:
                    return self._json({"error": "unknown id"}, 400)
                c = App.search.by_key.get(body.get("key", ""))
                url, alt, page = (c["url"], c["alt"], c["page"]) if c else (body.get("url", "").strip(), "", "")
                if not url:
                    return self._json({"error": "no image"}, 400)
                try:
                    data, _ = App.images.get(url, alt, page)
                    jpeg = ImageFetcher.to_jpeg(data)
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": f"download failed: {e}"[:400]}, 502)
                App.store.save_photo(item_id, jpeg, url, page)
                App.store.mark(item_id, "replaced")
                return self._json({"ok": True, "bytes": len(jpeg)})
            if u.path == "/api/upload":                # an image pasted from the clipboard
                item_id = body.get("id")
                if item_id not in App.store.by_id:
                    return self._json({"error": "unknown id"}, 400)
                try:
                    jpeg = ImageFetcher.to_jpeg(base64.b64decode(body.get("data", "")))
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": f"not an image: {e}"[:300]}, 400)
                App.store.save_photo(item_id, jpeg, "", body.get("from") or "pasted from clipboard")
                App.store.mark(item_id, "replaced")
                return self._json({"ok": True, "bytes": len(jpeg)})
            if u.path == "/api/push":
                return self._json(Git.push_images())
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 500)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer((Config.HOST, Config.PORT), Handler)
    url = f"http://{Config.HOST}:{Config.PORT}/"
    print(f"Tal picker running at {url}  (Ctrl+C to stop)")
    if Config.OPEN_BROWSER:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
