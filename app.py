#!/usr/bin/env python3
"""
Movie Mirror - Stremio catalog + subtitles addons (Flask).

Catalog addon ("Movie Mirror by Nandu10"):
    Malayalam-subtitled world cinema from moviemirrorsubtitles.com.
    Static data in mm_data.json (crawled from the site's post sitemap),
    posters/backdrops from TMDB, cards use IMDb tt IDs,
    names are "English / മലയാളം".

    GET /mm/manifest.json
    GET /mm/catalog/<type>/<id>.json[?skip=N]  (also /skip=N.json path form)
    GET /mm/meta/<type>/<id>.json             (tt..., tmdb:..., mm_... IDs)

Subtitle addon ("Movie Mirror Subtitles"):
    Malayalam subtitles proxied from moviemirrorsubtitles.com,
    cached locally (7d hits / 6h misses), UTF-8 normalized.

    GET /mm-sub/manifest.json
    GET /mm-sub/subtitles/movie/{tt}.json
    GET /mm-sub/subtitles/series/{tt}:{season}:{episode}.json
    GET /mm-sub/srt/{key}.srt

Deploy: set TMDB_API_KEY env var (only needed for crawl, not runtime),
run with gunicorn (see README). Install in Stremio/Nuvio with the
manifest URLs: <your-url>/mm/manifest.json and <your-url>/mm-sub/manifest.json
"""

import io
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import zipfile
from collections import Counter

from flask import Flask, jsonify, request, Response
from werkzeug.middleware.proxy_fix import ProxyFix

SITE = "https://moviemirrorsubtitles.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
POSTER = "https://image.tmdb.org/t/p/w500"
BG = "https://image.tmdb.org/t/p/w1280"

MM_VERSION = "1.0.0"
SUB_VERSION = "1.0.0"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ---------------------------------------------------------------- debug log
_req_log = []


@app.before_request
def _log_req():
    _req_log.append(request.method + " " + request.path)
    del _req_log[:-80]


@app.route("/debug/log")
def debug_log():
    out = list(_req_log)
    _req_log.clear()
    return jsonify({"requests": out})


@app.route("/")
def index():
    base = request.url_root.rstrip("/")
    return (
        "<h2>Movie Mirror Stremio addons</h2>"
        f"<p>Catalog: <code>{base}/mm/manifest.json</code></p>"
        f"<p>Subtitles: <code>{base}/mm-sub/manifest.json</code></p>"
    )


# ================================================================ CATALOG

_mm_data = None
_mm_metas = {}
_mm_defs = None
_mm_by_tt = None

TYPE_MARKERS = {"movies", "movie", "series", "tv series"}


def mm_load():
    global _mm_data
    if _mm_data is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "mm_data.json")
        with open(p, encoding="utf-8") as f:
            _mm_data = json.load(f)
    return _mm_data


def _genre_slug(g):
    return re.sub(r"[^a-z0-9]+", "_", g.lower()).strip("_") or "other"


def mm_catalog_defs():
    """New Releases, Movies, Series, then site genres ordered by count."""
    global _mm_defs
    if _mm_defs is not None:
        return _mm_defs
    data = mm_load()
    items = data.get("items", [])
    cats = [
        {"id": "mm_new", "type": "movie", "name": "Movie Mirror: New Releases",
         "kind": "new"},
        {"id": "mm_movies", "type": "movie", "name": "Movie Mirror: Movies",
         "kind": "media", "media": "movie"},
        {"id": "mm_series", "type": "series", "name": "Movie Mirror: Series",
         "kind": "media", "media": "series"},
    ]
    gc = Counter(g for it in items for g in it.get("genres", [])
                 if g.lower() not in TYPE_MARKERS)
    for genre, _ in gc.most_common():
        cats.append({"id": f"mm_genre_{_genre_slug(genre)}", "type": "movie",
                     "name": f"Movie Mirror: {genre}",
                     "kind": "genre", "genre": genre})
    _mm_defs = cats
    return cats


def _mm_items_for_cat(cat):
    items = mm_load().get("items", [])
    kind = cat["kind"]
    if kind == "new":
        return items  # sitemap order = newest first
    if kind == "media":
        return [it for it in items if it.get("type") == cat["media"]]
    if kind == "genre":
        return [it for it in items if cat["genre"] in it.get("genres", [])]
    return []


def _mm_card(it):
    disp = it["name_en"] + (f" / {it['name_ml']}"
                            if it.get("name_ml") and it["name_ml"] != it["name_en"]
                            else "")
    cid = it.get("imdb_id") or f"tmdb:{it.get('tmdb_id')}" or f"mm_{it['post_url']}"
    return {
        "id": cid,
        "type": "series" if it.get("type") == "series" else "movie",
        "name": disp,
        "poster": f"{POSTER}{it['poster_path']}" if it.get("poster_path") else None,
        "background": f"{BG}{it['backdrop_path']}" if it.get("backdrop_path") else None,
        "description": ((it.get("overview") or "")
                        + f"\n\n\U0001F4DD Malayalam subtitles: Movie Mirror"
                          f"\n{it['post_url']}").strip(),
        "releaseInfo": it.get("year") or "",
        "genres": it.get("genres", []),
    }


def mm_metas(cid):
    if cid in _mm_metas:
        return _mm_metas[cid]
    cat = next(c for c in mm_catalog_defs() if c["id"] == cid)
    items = _mm_items_for_cat(cat)
    metas = []
    seen_series_tt = set()
    for it in items:
        # one card per series (multiple season posts share the same tt)
        if it.get("type") == "series" and it.get("imdb_id"):
            if it["imdb_id"] in seen_series_tt:
                continue
            seen_series_tt.add(it["imdb_id"])
        metas.append(_mm_card(it))
    metas = [m for m in metas if m.get("poster")]
    _mm_metas[cid] = metas
    return metas


def mm_by_tt():
    global _mm_by_tt
    if _mm_by_tt is None:
        idx = {}
        for it in mm_load().get("items", []):
            if it.get("imdb_id"):
                # series: keep list (one post per season, same tt)
                if it.get("type") == "series":
                    cur = idx.get(it["imdb_id"])
                    if isinstance(cur, list):
                        cur.append(it)
                    elif isinstance(cur, dict):
                        idx[it["imdb_id"]] = [cur, it]
                    else:
                        idx[it["imdb_id"]] = [it]
                else:
                    idx.setdefault(it["imdb_id"], it)
            if it.get("tmdb_id"):
                idx[f"tmdb:{it['tmdb_id']}"] = it
        _mm_by_tt = idx
    return _mm_by_tt


def _mm_pick_series(items, season):
    """Pick the season post matching the requested season number."""
    if not isinstance(items, list):
        items = [items]
    if season is None:
        return items[0]
    s2 = "%02d" % season
    for it in items:
        blob = ((it.get("sub_url") or "") + " " + it.get("name_en", "")).lower()
        if f"s{s2}" in blob or f"season {season}" in blob or f"season{s2}" in blob:
            return it
    return items[0]


@app.route("/mm/meta/<mtype>/<mid>.json")
def mm_meta(mtype, mid):
    # catalog-level meta so "see all" never hits a dead end
    cat = next((c for c in mm_catalog_defs() if c["id"] == mid), None)
    if cat:
        items = _mm_items_for_cat(cat)
        first = items[0] if items else None
        top = "\n".join(
            f"\u2022 {it['name_en']}"
            + (f" / {it['name_ml']}"
               if it.get("name_ml") and it["name_ml"] != it["name_en"] else "")
            + (f" ({it['year']})" if it.get("year") else "")
            for it in items[:8]
        )
        meta = {
            "id": cat["id"],
            "type": cat["type"],
            "name": cat["name"],
            "poster": f"{POSTER}{first['poster_path']}"
                      if first and first.get("poster_path") else None,
            "background": f"{BG}{first['backdrop_path']}"
                          if first and first.get("backdrop_path") else None,
            "description": (f"{len(items)} titles in {cat['name']}.\n\n"
                            f"Top titles:\n{top}"
                            f"\n\n\U0001F4DD Malayalam subtitles: Movie Mirror").strip(),
        }
        return jsonify({"meta": {k: v for k, v in meta.items() if v}})
    # mid: tt..., tmdb:..., or mm_... ; anything else -> graceful empty
    if mid.startswith("tt"):
        entry = mm_by_tt().get(mid)
        it = _mm_pick_series(entry, None) if entry else None
    elif mid.startswith("tmdb:"):
        it = mm_by_tt().get(mid)
    else:
        it = None
    if not it:
        return jsonify({"meta": {}})
    card = _mm_card(it)
    card["id"] = mid  # echo the requested ID format
    return jsonify({"meta": {k: v for k, v in card.items() if v}})


@app.route("/mm/manifest.json")
def mm_manifest():
    return jsonify({
        "id": "com.moviemirror.catalog",
        "version": MM_VERSION,
        "name": "Movie Mirror by Nandu10",
        "description": "Movie Mirror — world cinema with Malayalam subtitles "
                       "(moviemirrorsubtitles.com)",
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "tmdb:", "mm_"],
        "resources": ["catalog", "meta"],
        "catalogs": [
            {"type": c["type"], "id": c["id"], "name": c["name"],
             "extra": [{"name": "skip", "isRequired": False}]}
            for c in mm_catalog_defs()
        ],
    })


@app.route("/mm/catalog/<ctype>/<cid>.json")
@app.route("/mm/catalog/<ctype>/<cid>/skip=<int:skip>.json")
def mm_catalog(ctype, cid, skip=0):
    cat = next((c for c in mm_catalog_defs() if c["id"] == cid), None)
    if not cat:
        return jsonify({"metas": []}), 404
    try:
        metas = mm_metas(cid)
    except Exception as e:
        return jsonify({"metas": [], "error": str(e)}), 502
    if "skip" not in request.view_args:
        try:
            skip = int(request.args.get("skip", "0"))
        except ValueError:
            skip = 0
    return jsonify({"metas": metas[skip:skip + 20]})


# ============================================================== SUBTITLES

SUB_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "sub_cache")
SUB_INDEX = os.path.join(SUB_CACHE_DIR, "index.json")
SUB_TTL = 7 * 24 * 3600   # subtitle bytes rarely change
MISS_TTL = 6 * 3600       # remember misses for 6h
SITE_GAP = 1.5            # politeness gap for moviemirrorsubtitles.com

os.makedirs(SUB_CACHE_DIR, exist_ok=True)

_sub_lock = threading.Lock()
_sub_last_fetch = 0.0


def _sub_fetch(url, referer=None, timeout=30, retries=3):
    """Polite GET with browser UA. Uses curl subprocess: Python's ssl
    module can hang on this host in long-running processes."""
    global _sub_last_fetch
    last_err = None
    for attempt in range(retries):
        with _sub_lock:
            wait = SITE_GAP - (time.time() - _sub_last_fetch)
            if wait > 0 and SITE in url:
                time.sleep(wait)
            try:
                cmd = ["curl", "-s", "--max-time", str(timeout), "-A", UA, url]
                if referer:
                    cmd += ["-e", referer]
                r = subprocess.run(cmd, capture_output=True,
                                   timeout=timeout + 10)
                if r.returncode != 0 or not r.stdout:
                    raise IOError(f"curl rc={r.returncode}")
                return r.stdout
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
            finally:
                if SITE in url:
                    _sub_last_fetch = time.time()
    raise IOError(f"_sub_fetch failed after {retries}: {last_err}")


def _sub_index_load():
    try:
        with open(SUB_INDEX, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _sub_index_save(idx):
    tmp = SUB_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f)
    os.replace(tmp, SUB_INDEX)


def _sub_key(rid):
    return re.sub(r"[^A-Za-z0-9]+", "_", rid).strip("_")


def _to_utf8(raw):
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(enc).encode("utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace").encode("utf-8")


def _pick_from_zip(data, season=None, episode=None):
    """Extract the right .srt from a zip. For series, match SXXEXX."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist()
                 if n.lower().endswith((".srt", ".ass", ".vtt"))
                 and not n.startswith("__MACOSX")]
        if not names:
            return None
        if season is not None and episode is not None:
            pat = re.compile(r"s%02d\s*e%02d" % (season, episode), re.I)
            for n in names:
                if pat.search(n):
                    return z.read(n)
            pat2 = re.compile(r"%dx%02d" % (season, episode), re.I)
            for n in names:
                if pat2.search(n):
                    return z.read(n)
            return None  # specific episode requested but not in zip
        names.sort(key=lambda n: (0 if n.lower().endswith(".srt") else 1, n))
        return z.read(names[0])
    return None


def _sub_resolve(rid, imdb_id, season=None, episode=None):
    """Returns (srt_bytes, sub_url) or None. Uses cache, then live fetch."""
    key = _sub_key(rid)
    idx = _sub_index_load()
    now = time.time()
    entry = idx.get(key)
    if entry:
        if entry.get("miss") and now - entry["at"] < MISS_TTL:
            return None
        path = os.path.join(SUB_CACHE_DIR, key + ".srt")
        if not entry.get("miss") and now - entry["at"] < SUB_TTL \
                and os.path.exists(path):
            with open(path, "rb") as f:
                return f.read(), entry.get("sub_url")
    # live lookup: find the post by IMDb id (season-aware for series)
    entry = mm_by_tt().get(imdb_id)
    item = _mm_pick_series(entry, season) if entry else None
    if not item or not item.get("sub_url"):
        idx[key] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    sub_url = item["sub_url"]
    try:
        raw = _sub_fetch(sub_url, referer=item["post_url"])
        if sub_url.lower().split("?")[0].endswith(".zip") or raw[:2] == b"PK":
            srt = _pick_from_zip(raw, season, episode)
            if not srt:
                raise ValueError("no matching srt in zip")
        else:
            srt = raw
        srt = _to_utf8(srt)
    except Exception:
        idx[key] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    with open(os.path.join(SUB_CACHE_DIR, key + ".srt"), "wb") as f:
        f.write(srt)
    idx[key] = {"at": now, "sub_url": sub_url}
    _sub_index_save(idx)
    return srt, sub_url


@app.route("/mm-sub/manifest.json")
def mm_sub_manifest():
    return jsonify({
        "id": "com.moviemirror.subtitles",
        "version": SUB_VERSION,
        "name": "Movie Mirror Subtitles",
        "description": "Malayalam subtitles from moviemirrorsubtitles.com "
                       "(Movie Mirror). Subtitles only — video comes from "
                       "your own sources.",
        "resources": ["subtitles"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": [],
    })


@app.route("/mm-sub/subtitles/<vtype>/<rid>.json")
def mm_sub_subtitles(vtype, rid):
    rid = urllib.parse.unquote(rid)
    parts = rid.split(":")
    imdb_id = parts[0]
    if not re.match(r"^tt\d+$", imdb_id):
        return jsonify({"subtitles": []})
    season = episode = None
    if vtype == "series" and len(parts) >= 3:
        try:
            season, episode = int(parts[1]), int(parts[2])
        except ValueError:
            return jsonify({"subtitles": []})
    if not _sub_resolve(rid, imdb_id, season, episode):
        return jsonify({"subtitles": []})
    base = request.url_root.rstrip("/")
    return jsonify({"subtitles": [{
        "id": "mmsub:" + _sub_key(rid),
        "url": f"{base}/mm-sub/srt/{_sub_key(rid)}.srt",
        "lang": "mal",
    }]})


@app.route("/mm-sub/srt/<key>.srt")
def mm_sub_srt(key):
    if not re.match(r"^[A-Za-z0-9_]+$", key):
        return jsonify({"error": "not found"}), 404
    path = os.path.join(SUB_CACHE_DIR, key + ".srt")
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="text/plain; charset=utf-8",
                    headers={"Access-Control-Allow-Origin": "*"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
