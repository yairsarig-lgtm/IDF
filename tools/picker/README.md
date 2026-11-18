# Tal photo picker

A local tool for going through the items one by one and keeping or replacing each photo.

## Start

```
python tools/picker/server.py
```

The picker opens at http://127.0.0.1:8765 in your browser. Press Ctrl+C in the terminal to stop it.
You need Python 3 and Pillow (`pip install pillow`).

## Keys

| Key | What it does |
|---|---|
| Enter | Keep the current photo and go to the next item |
| 1–6 or click | Use that candidate: download it, save it as JPG (longest side 1400px) to `images/<id>.jpg`, update `manifest.json` and `credits.json` |
| ← | Go back one item |
| → | Skip without marking the item |
| M | Load the next 6 results |
| / | Jump to the search box |
| Ctrl+V | Paste a copied image, or a copied image URL, as this item's photo |

You can also edit the search box, use the "באנגלית" button (shown when the item has an `en` hint),
paste any image URL into the field, filter the list, and push. "העלאה ל-GitHub" commits only `images/` and pushes it to `main`.

When the results are poor, "Google ↗" opens Google Images for the same query in a new tab. Right-click a
photo there, choose "Copy image", come back to the picker and press Ctrl+V.

The items you've reviewed are saved in `tools/picker/state/reviewed.json`. That file stays on this PC and is
never committed, so you can stop and continue later.

## Search

Searches run from the local server and try these sources in order:

1. **DuckDuckGo images**. It usually answers scripts with a bot check. When it does, it's skipped for the rest of the session.
2. **Bing images**. This gives the best results for brands and snacks. After many searches in a row, Bing
   starts hiding most of its results from this PC, and recovers after a pause.
3. **Wikimedia Commons**. Its official API never blocks, but it has few brand photos and finds little for Hebrew queries.

If a source finds nothing (or almost nothing), the next one's results are added. If the Hebrew name finds
nothing at all and the item has an `en` hint, the English search runs automatically. Results are ranked so
that landscape photos at least 800px wide, in JPG, come first. The source used is shown under the grid.

To use Google instead, set two environment variables before starting the picker. Google is then tried first:

```
$env:PICKER_GOOGLE_KEY = "<API key>"
$env:PICKER_GOOGLE_CX  = "<Programmable Search Engine id, with image search on>"
```

Settings such as the port, the size limit and the JPG quality are in `Config` at the top of `server.py`.
