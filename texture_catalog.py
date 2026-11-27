"""The PS2 texture-pack catalog (github.com/ps2ktxpak/catalog, MIT licence).

A hand-maintained record of ~1,000 PS2 texture packs: which game and disc
serials each is for, who made it, whether it is free or paid, and where it is
downloaded from. It's the most complete index there is, and it's keyed by disc
serial, which is what makes a match certain rather than a search-engine guess.

The whole catalog is one ~1.3 MB zip; it's fetched at most once a week and
cached as compact JSON. Qt-free; network access goes through `getter` so tests
never touch the network.

How an entry becomes something PCTX can offer:
* free, and downloadable from GitHub or the Internet Archive -> installed
  automatically (it's still checked for a real .zip/.7z before it's offered);
* free, but hosted somewhere PCTX can't fetch from (Google Drive, Mega,
  MediaFire, ...) -> "opens in your browser";
* paid -> only ever a link to the creator's own page. PCTX never downloads a
  paid pack from anywhere.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from typing import Callable, Dict, List, Optional

CATALOG_ZIP = "https://codeload.github.com/ps2ktxpak/catalog/zip/refs/heads/main"
CATALOG_PAGE = "https://ps2ktxpak.github.io/catalog/"
MAX_AGE = 7 * 24 * 3600
_CACHE_NAME = "texture_catalog.json"


def _http_get(url: str, timeout: int = 60) -> bytes:
    import requests
    r = requests.get(url, headers={"User-Agent": "PCTX (PCSX2 companion app)"}, timeout=timeout)
    r.raise_for_status()
    return r.content


def _norm_serial(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _slim(d: dict) -> Optional[dict]:
    """The few fields PCTX uses, from one pack YAML document."""
    if not isinstance(d, dict) or not d.get("key"):
        return None
    game = d.get("game") or {}
    dl = d.get("download") or {}
    src = next((s for s in d.get("sources") or [] if s.get("primary")), None) or (d.get("sources") or [{}])[0]
    return {
        "key": d["key"],
        "name": d.get("name") or game.get("title") or d["key"],
        "title": game.get("title") or "",
        "serials": [_norm_serial(s) for s in game.get("serials") or [] if s],
        "creators": [c.get("creator") for c in d.get("credits") or [] if c.get("creator")],
        "type": d.get("type") or "",
        "completeness": d.get("completeness") or "",
        "cost": (d.get("access") or {}).get("cost") or "",
        "dl_url": dl.get("url") or "",
        "dl_host": dl.get("host") or "",
        "dl_kind": dl.get("kind") or "",
        "dl_status": dl.get("status") or "",
        "source_url": (src or {}).get("url") or "",
    }


def parse_zip(data: bytes) -> List[dict]:
    import yaml  # PyYAML; only needed when the cache is refreshed
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if "/data/packs/" in name and name.endswith((".yaml", ".yml")):
                try:
                    rec = _slim(yaml.safe_load(z.read(name)))
                except Exception:
                    continue
                if rec:
                    out.append(rec)
    return out


def load(cache_dir: str, force: bool = False, getter: Callable = _http_get, now: Optional[float] = None):
    """(packs, error). Uses the weekly cache; falls back to a stale cache if the
    download fails, so a flaky connection never loses what we knew."""
    now = time.time() if now is None else now
    path = os.path.join(cache_dir, _CACHE_NAME)
    cached = None
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError):
        pass
    if cached and not force and now - cached.get("fetched", 0) < MAX_AGE:
        return cached.get("packs", []), None
    try:
        packs = parse_zip(getter(CATALOG_ZIP))
        if not packs:
            raise ValueError("the catalog was empty")
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched": now, "packs": packs}, f)
        os.replace(tmp, path)
        return packs, None
    except Exception as e:
        if cached:
            return cached.get("packs", []), f"couldn't refresh the texture-pack catalog ({e}); using the copy from before"
        return [], f"couldn't download the texture-pack catalog ({e})"


def _title_key(t: str) -> str:
    from enhancements import normalize_title
    return normalize_title(t)


def find(packs: List[dict], serial: str, title: str = "") -> List[dict]:
    """Packs for this game: every pack listing the disc's serial, plus packs that
    list no serials at all but have exactly the same (normalised) title."""
    s = _norm_serial(serial)
    tk = _title_key(title) if title else ""
    hits = [p for p in packs if s and s in p["serials"]]
    if tk:
        hits += [p for p in packs if not p["serials"] and p["title"] and _title_key(p["title"]) == tk and p not in hits]
    return hits


def to_candidate(p: dict, serial_match: bool) -> Optional[dict]:
    """A texture candidate in PCTX's usual shape, or None if there's nothing usable."""
    who = ", ".join(p["creators"]) or "unknown author"
    base = {"name": f"{p['name']} by {who}", "catalog_key": p["key"], "verified": True,
            "confidence": "catalog" if serial_match else "catalog_title",
            "description": " · ".join(x for x in (p["type"].replace("_", " "), p["completeness"]) if x)}
    page = p["source_url"] or CATALOG_PAGE
    if p["cost"] == "paid":
        return dict(base, name=base["name"] + " (paid)", manual_url=page, paid=True)
    if p["dl_status"] in ("dead", "not_found"):
        return None
    url = p["dl_url"]
    if p["dl_host"] == "archive_org" and p["dl_kind"] == "file":
        m = re.search(r"archive\.org/(?:details|download)/([^/?#]+)", url)
        if m:
            return dict(base, archive_org_id=m.group(1))
    if p["dl_host"] == "github" and p["dl_kind"] in ("file", "release"):
        m = re.search(r"github\.com/([^/]+/[^/?#]+)", url)
        if m:
            return dict(base, github_repo=m.group(1).removesuffix(".git"))
    if url and p["dl_status"] == "resolved":
        return dict(base, manual_url=url)
    if p["source_url"]:
        return dict(base, manual_url=p["source_url"])
    return None


def candidates_for(packs: List[dict], serial: str, title: str = "") -> List[dict]:
    s = _norm_serial(serial)
    out = []
    for p in find(packs, serial, title):
        c = to_candidate(p, s in p["serials"])
        if c:
            out.append(c)
    # automatic downloads first, then browser links, then paid pages
    out.sort(key=lambda c: (bool(c.get("paid")), bool(c.get("manual_url")), c["confidence"] != "catalog"))
    return out
