#!/usr/bin/env python3
"""Copy the item list from data/items.json into index.html (the line that ends with //ITEMS)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
items = json.loads((ROOT / "data" / "items.json").read_text(encoding="utf-8"))
slim = [{"id": i["id"], "name": i["name"]} for i in items]
page = ROOT / "index.html"
html = page.read_text(encoding="utf-8")
line = "const ITEMS=" + json.dumps(slim, ensure_ascii=False, separators=(",", ":")) + ";//ITEMS"
html, n = re.subn(r"^const ITEMS=.*//ITEMS$", lambda _: line, html, count=1, flags=re.M)
assert n == 1, "ITEMS line not found"
page.write_text(html, encoding="utf-8")
print(len(slim), "items written into index.html")
