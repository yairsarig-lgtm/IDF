# Tal photo picker

A local tool for going through the items one by one and choosing each photo from Google Images results.

## Start

```
python tools/picker/server.py
```

The picker opens at http://127.0.0.1:8765 in your browser. Press Ctrl+C in the terminal to stop it.
You need Python 3 and Pillow (`pip install pillow`).

## Serper key

Searches use the [Serper](https://serper.dev) API, which returns Google Images results. Put the key in
`tools/picker/state/serper_key.txt`. You can instead set the `PICKER_SERPER_KEY` environment variable.
The `state/` folder is git-ignored, so the key is never committed. If the key is missing or Serper fails,
the page shows a message instead of results.

The picker searches with the item's Hebrew name, country Israel (`gl=il`) and language Hebrew (`hl=iw`).
Each query asks for 30 results and costs 2 credits, because more than 10 results costs double. That one
query covers 5 pages of "More". The next item's results are loaded ahead of time, so expect roughly one
query per item. The page header shows how many queries and credits have been used. The count is kept in
`state/serper_usage.json`.

## Keys

| Key | What it does |
|---|---|
| 1–6 or click | Use that photo: download it, save it as JPG (longest side 1400px) to `images/<id>.jpg`, update `manifest.json` and `credits.json`, and go to the next item |
| M | Show the next 6 results |
| → | Skip the item |
| ← | Go back one item |
| / | Jump to the search box, to change the query |
| Ctrl+V | Paste a copied image, or a copied image URL, as this item's photo |

When none of the results fit, "Google ↗" opens Google Images in a new tab. Right-click a photo there,
choose "Copy image", come back and press Ctrl+V. You can also paste an image URL into the field.

Items are shown in this order: the three force items first, then the 500 items in the order of `data/items.json`.
The picker resumes at the first item you haven't chosen or skipped. Progress is kept in
`state/reviewed.json`; delete that file to start over. "העלאה ל-GitHub" commits only `images/` and pushes it to `main`.

Settings such as the port, the number of results, the size limit and the JPG quality are in `Config` at the
top of `server.py`.
