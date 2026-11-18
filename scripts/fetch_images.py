#!/usr/bin/env python3
"""Download one freely licensed photo per item into images/.

Sources, in order: the lead photo of the matching Hebrew Wikipedia article, then a Wikimedia Commons
search. Only freely licensed files are used. Every picked file is logged in images/credits.json so a
wrong match is easy to spot and replace.

Run by the "Fetch images" GitHub Action (or locally: pip install requests pillow; python scripts/fetch_images.py).
Set REFETCH=1 to replace photos that already exist.
"""
import io
import json
import os
import sys
import time
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
IMG = ROOT / "images"
CAND = IMG / "force"
REFETCH = os.environ.get("REFETCH") == "1"
WIDTH = 1280

S = requests.Session()
S.headers["User-Agent"] = "TalAppImageFetcher/1.0 (https://github.com/yairsarig-lgtm/IDF; personal project)"

# Photos for the three forced items: several candidates each, so the best one can be picked by eye.
FORCE = {
    "force-1": ["open paint cans colors", "paint cans colorful", "paint buckets colours"],
    "force-2": ["sunscreen bottle", "sunscreen lotion tube", "sun cream bottle beach"],
    "force-3": ["Flag of Israel waving", "Israeli flag flying", "Israel flag flagpole sky"],
}


def get(url, **kw):
    for attempt in range(5):
        r = S.get(url, timeout=40, **kw)
        if r.status_code in (429, 503):
            time.sleep(int(r.headers.get("Retry-After", 5)) + attempt * 3)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def api(host, **params):
    params.update(format="json", formatversion=2)
    return get(f"https://{host}/w/api.php", params=params).json()


def hewiki(q):
    d = api("he.wikipedia.org", action="query", generator="search", gsrsearch=q, gsrlimit=4,
            prop="pageimages", piprop="thumbnail|name", pithumbsize=WIDTH, pilicense="free")
    pages = sorted(d.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    out = []
    for p in pages:
        t, name = p.get("thumbnail"), p.get("pageimage", "")
        if t:
            out.append({"url": t["source"], "file": name, "svg": name.lower().endswith(".svg"),
                        "from": "he.wikipedia.org/wiki/" + p["title"].replace(" ", "_")})
    return out


def commons(q, limit=8):
    d = api("commons.wikimedia.org", action="query", generator="search", gsrnamespace=6,
            gsrsearch=q + " filetype:bitmap", gsrlimit=limit, prop="imageinfo",
            iiprop="url|mime|size", iiurlwidth=WIDTH)
    pages = sorted(d.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    out = []
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime") != "image/jpeg" or ii.get("width", 0) < 640:
            continue
        w, h = ii.get("width", 1), ii.get("height", 1)
        if not 0.5 <= w / h <= 2.4:  # skip panoramas and tall scans
            continue
        out.append({"url": ii.get("thumburl") or ii["url"], "file": p["title"], "svg": False,
                    "from": "commons.wikimedia.org/wiki/" + p["title"].replace(" ", "_")})
    return out


def save(url, dest):
    im = Image.open(io.BytesIO(get(url).content))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    im.thumbnail((1400, 1400))
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "JPEG", quality=84, optimize=True)


def pick(item):
    q = item.get("q") or item["name"]
    cands = []
    try:
        cands = hewiki(q)
    except Exception as e:  # noqa: BLE001
        print("  hewiki error:", e)
    photos = [c for c in cands[:2] if not c["svg"]]
    if photos:
        return photos[0]
    try:
        found = commons(item.get("en") or item["name"])
        if found:
            return found[0]
    except Exception as e:  # noqa: BLE001
        print("  commons error:", e)
    return cands[0] if cands else None  # a logo or drawing is better than nothing


def main():
    items = json.loads((ROOT / "data" / "items.json").read_text(encoding="utf-8"))
    credits_path = IMG / "credits.json"
    credits = json.loads(credits_path.read_text(encoding="utf-8")) if credits_path.exists() else {}
    IMG.mkdir(exist_ok=True)
    missing = []
    for n, item in enumerate(items, 1):
        dest = IMG / f"{item['id']}.jpg"
        if dest.exists() and not REFETCH:
            continue
        c = pick(item)
        if not c:
            missing.append(item)
            print(f"[{n}/{len(items)}] {item['name']}: nothing found")
            continue
        try:
            save(c["url"], dest)
            credits[item["id"]] = {"name": item["name"], "file": c["file"], "from": c["from"]}
            print(f"[{n}/{len(items)}] {item['name']}: {c['file']}")
        except Exception as e:  # noqa: BLE001
            missing.append(item)
            print(f"[{n}/{len(items)}] {item['name']}: download failed ({e})")
        time.sleep(0.15)

    for fid, queries in FORCE.items():
        seen, k = set(), 0
        for q in queries:
            try:
                found = commons(q, limit=6)
            except Exception as e:  # noqa: BLE001
                print("  commons error:", e)
                continue
            for c in found:
                if c["file"] in seen or k >= 9:
                    continue
                seen.add(c["file"])
                dest = CAND / f"{fid}-{chr(97 + k)}.jpg"
                if dest.exists() and not REFETCH:
                    k += 1
                    continue
                try:
                    save(c["url"], dest)
                    credits[dest.stem] = {"name": fid, "file": c["file"], "from": c["from"]}
                    k += 1
                except Exception as e:  # noqa: BLE001
                    print("  candidate failed:", e)
        first = CAND / f"{fid}-a.jpg"
        main_img = IMG / f"{fid}.jpg"
        if first.exists() and not main_img.exists():
            main_img.write_bytes(first.read_bytes())
        print(f"{fid}: {k} candidates")

    have = sorted(p.stem for p in IMG.glob("*.jpg"))
    (IMG / "manifest.json").write_text(json.dumps(have), encoding="utf-8")
    credits_path.write_text(json.dumps(credits, ensure_ascii=False, indent=1), encoding="utf-8")
    (IMG / "missing.txt").write_text("\n".join(f"{m['id']}\t{m['name']}" for m in missing) + "\n", encoding="utf-8")
    print(f"done: {len(have)} photos, {len(missing)} missing")


if __name__ == "__main__":
    sys.exit(main())
