"""
Nandus - Stremio catalog addon (TMDB powered).

Row order (user spec 2026-10-03):
  1. Streaming (OTT icon row)
  2. Latest Malayalam Movies, 3. Latest Tamil Movies, 4. Latest Korean Movies,
  5. Latest Korean Series, 6. Top Rated Korean Movies, 7. Top Rated Korean Series,
  8. Latest Tamil Series, 9. Latest Malayalam Series,
  10. Latest Tamil Dubbed Movies, 11. Latest Tamil Dubbed Series,
  12. Tamil Dubbed Movies, 13. Tamil Dubbed Series,
  14. K-Drama, 15. Documentaries

Deploy: set TMDB_API_KEY env var, run with gunicorn (see README).
Install in Stremio/Nuvio: <your-url>/manifest.json
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
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from werkzeug.middleware.proxy_fix import ProxyFix

import requests
from flask import Flask, jsonify, request, Response

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
TMDB = "https://api.themoviedb.org/3"
POSTER = "https://image.tmdb.org/t/p/w500"
BG = "https://image.tmdb.org/t/p/w1280"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ---- request debug log (to diagnose what the Nuvio app actually requests) ----
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


def tile_base():
    base = os.environ.get("TILE_BASE", "").strip()
    if base:
        return base.rstrip("/")
    return request.host_url.rstrip("/")


CATALOGS = [
    {"id": "ott_icons", "type": "movie", "name": "Streaming",
     "media": "movie", "mode": "ott_icons"},
    {"id": "ml_movies", "type": "movie", "name": "Latest Malayalam Movies",
     "media": "movie", "mode": "latest", "lang": "ml"},
    {"id": "ta_movies", "type": "movie", "name": "Latest Tamil Movies",
     "media": "movie", "mode": "latest", "lang": "ta"},
    {"id": "ko_movies", "type": "movie", "name": "Latest Korean Movies",
     "media": "movie", "mode": "latest", "lang": "ko"},
    {"id": "ko_series", "type": "series", "name": "Latest Korean Series",
     "media": "tv", "mode": "latest", "lang": "ko"},
    {"id": "ko_top_movies", "type": "movie", "name": "Top Rated Korean Movies",
     "media": "movie", "mode": "top_rated", "lang": "ko"},
    {"id": "ko_top_series", "type": "series", "name": "Top Rated Korean Series",
     "media": "tv", "mode": "top_rated", "lang": "ko"},
    {"id": "ta_series", "type": "series", "name": "Latest Tamil Series",
     "media": "tv", "mode": "latest", "lang": "ta"},
    {"id": "ml_series", "type": "series", "name": "Latest Malayalam Series",
     "media": "tv", "mode": "latest", "lang": "ml"},
    {"id": "ta_dubbed_latest", "type": "movie", "name": "Latest Tamil Dubbed Movies",
     "media": "movie", "mode": "dubbed", "lang": "ta", "sort": "release"},
    {"id": "ta_dubbed_series_latest", "type": "series", "name": "Latest Tamil Dubbed Series",
     "media": "tv", "mode": "dubbed", "lang": "ta", "sort": "release"},
    {"id": "ta_dubbed", "type": "movie", "name": "Tamil Dubbed Movies",
     "media": "movie", "mode": "dubbed", "lang": "ta", "sort": "popularity"},
    {"id": "ta_dubbed_series", "type": "series", "name": "Tamil Dubbed Series",
     "media": "tv", "mode": "dubbed", "lang": "ta", "sort": "popularity"},
    {"id": "kdrama", "type": "series", "name": "K-Drama",
     "media": "tv", "mode": "kdrama"},
    {"id": "doc_movies", "type": "movie", "name": "Documentaries",
     "media": "movie", "mode": "genre", "genre_id": 99},
]

# OTT providers for the "Streaming" icon row + meta pages.
# (name, tmdb_provider_id, watch_region, slug)
OTT_PROVIDERS = [
    ("JioHotstar", 2336, "IN", "jiohotstar"),
    ("Zee5", 232, "IN", "zee5"),
    ("Sony Liv", 237, "IN", "sonyliv"),
    ("Sun NXT", 309, "IN", "sunnxt"),
    ("Netflix", 8, "IN", "netflix"),
    ("Prime Video", 119, "IN", "primevideo"),
    ("aha", 532, "IN", "aha"),
    ("Manorama Max", 482, "IN", "manoramamax"),
    ("Discovery+", 510, "IN", "discoveryplus"),
    ("Lionsgate Play", 561, "IN", "lionsgateplay"),
    ("MX Player", 515, "IN", "mxplayer"),
    ("Tata Play", 502, "IN", "tataplay"),
    ("Rakuten Viki", 344, "US", "viki"),
]


def tmdb_get(path, params):
    params = dict(params or {})
    params["api_key"] = TMDB_API_KEY
    r = requests.get(f"{TMDB}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_dubbed(lang, skip, media="movie", sort="popularity", per=20):
    """Non-native-language titles (likely to have dubbed versions)."""
    page_idx = skip // per
    need = (page_idx + 1) * per
    if media == "movie":
        sort_key = "popularity.desc" if sort == "popularity" else "primary_release_date.desc"
        date_param = {"primary_release_date.lte": date.today().isoformat()}
    else:
        sort_key = "popularity.desc" if sort == "popularity" else "first_air_date.desc"
        date_param = {"first_air_date.lte": date.today().isoformat()}
    raw = []
    pg = 1
    try:
        while pg <= 10:
            params = {"sort_by": sort_key, "page": pg, "include_adult": "false",
                      "vote_count.gte": 20}
            params.update(date_param)
            data = tmdb_get(f"/discover/{media}", params)
            batch = data.get("results", [])
            if not batch:
                break
            raw.extend(batch)
            pg += 1
            if len([x for x in raw if x.get("original_language") != lang]) >= need:
                break
    except Exception as e:
        return [], str(e)
    filt = [x for x in raw if x.get("original_language") != lang]
    return filt[page_idx * per:(page_idx + 1) * per], None


_genre_cache = {}
_provider_cache = {}


def get_providers(media, tid):
    """Streaming (subscription) provider names for India, cached per item."""
    key = f"{media}:{tid}"
    if key in _provider_cache:
        return _provider_cache[key]
    try:
        data = tmdb_get(f"/{media}/{tid}/watch/providers", {})
        regions = data.get("results", {}) or {}
        region = regions.get("IN") or regions.get("US") or {}
        names = [p["provider_name"] for p in region.get("flatrate", [])
                 if p.get("provider_name")]
    except Exception:
        names = []
    _provider_cache[key] = names
    return names


def genre_names(media, ids):
    if media not in _genre_cache:
        try:
            data = tmdb_get(f"/genre/{media}/list", {"language": "en-US"})
            _genre_cache[media] = {g["id"]: g["name"] for g in data.get("genres", [])}
        except Exception:
            _genre_cache[media] = {}
    gmap = _genre_cache[media]
    return [gmap[i] for i in (ids or []) if i in gmap]


def to_meta(item, ctype, media):
    tid = item.get("id")
    title = item.get("title") or item.get("name") or "Unknown"
    year = (item.get("release_date") or item.get("first_air_date") or "")[:4]
    poster = item.get("poster_path")
    backdrop = item.get("backdrop_path")
    return {
        "id": f"tmdb:{tid}",
        "type": ctype,
        "name": title,
        "poster": f"{POSTER}{poster}" if poster else None,
        "background": f"{BG}{backdrop}" if backdrop else None,
        "description": item.get("overview") or "",
        "releaseInfo": year,
        "genres": genre_names(media, item.get("genre_ids")),
    }


def build_manifest():
    return {
        "id": "com.nandus.catalog",
        "version": "2.0.1",
        "name": "Nandus",
        "description": "Malayalam, Tamil & Korean catalogs + Streaming OTT row (TMDB)",
        "types": ["movie", "series"],
        "idPrefixes": ["tmdb:", "ott:", "ott_icons", "ml_movies", "ml_series",
                         "ta_movies", "ta_series", "ko_movies", "ko_series",
                         "ko_top_movies", "ko_top_series", "ta_dubbed",
                         "ta_dubbed_latest", "ta_dubbed_series",
                         "ta_dubbed_series_latest", "kdrama", "doc_movies"],
        "resources": [
            "catalog",
            {"name": "meta", "types": ["movie", "series"], "idPrefixes": ["ott:"]},
        ],
        "catalogs": [
            {
                "type": c["type"],
                "id": c["id"],
                "name": c["name"],
                "extra": [{"name": "skip", "isRequired": False}],
            }
            for c in CATALOGS
        ],
    }


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/manifest.json")
def manifest():
    return jsonify(build_manifest())


def _discover(media, params, page, offset):
    params = dict(params)
    params.update({"page": page, "include_adult": "false"})
    data = tmdb_get(f"/discover/{media}", params)
    return data.get("results", [])[offset:]


@app.route("/catalog/<ctype>/<cid>.json")
@app.route("/catalog/<ctype>/<cid>/<extra>.json")
def catalog(ctype, cid, extra=""):
    if not TMDB_API_KEY:
        return jsonify({"metas": [], "error": "TMDB_API_KEY not configured"}), 500

    cat = next((c for c in CATALOGS if c["id"] == cid and c["type"] == ctype), None)
    if not cat:
        return jsonify({"metas": []}), 404

    try:
        skip = int(request.args.get("skip", "0"))
    except ValueError:
        skip = 0
    page = skip // 20 + 1
    offset = skip % 20
    mode = cat["mode"]
    media = cat["media"]

    try:
        if mode == "ott_icons":
            base = tile_base()
            metas = [{
                "id": f"ott:{slug}",
                "type": "movie",
                "name": name,
                "poster": f"{base}/static/ott/{slug}.png",
                "background": f"{base}/static/ott/{slug}.png",
                "description": f"Tap to see what's streaming on {name}.",
            } for name, _pid, _region, slug in OTT_PROVIDERS]
            return jsonify({"metas": metas})

        if mode == "dubbed":
            results, err = fetch_dubbed(cat["lang"], skip, media,
                                        cat.get("sort", "popularity"))
            if err:
                return jsonify({"metas": [], "error": err}), 502
        elif mode == "latest":
            params = {
                "with_original_language": cat["lang"],
                "sort_by": "primary_release_date.desc" if media == "movie" else "first_air_date.desc",
                "vote_count.gte": 3,
            }
            if media == "movie":
                params["primary_release_date.lte"] = date.today().isoformat()
                params["primary_release_date.gte"] = "2023-01-01"
            else:
                params["first_air_date.lte"] = date.today().isoformat()
                params["first_air_date.gte"] = "2023-01-01"
            results = _discover(media, params, page, offset)
        elif mode == "top_rated":
            results = _discover(media, {
                "with_original_language": cat["lang"],
                "sort_by": "vote_average.desc",
                "vote_count.gte": 100,
            }, page, offset)
        elif mode == "kdrama":
            results = _discover(media, {
                "with_original_language": "ko",
                "with_genres": 18,
                "sort_by": "popularity.desc",
                "vote_count.gte": 10,
            }, page, offset)
        elif mode == "genre":
            results = _discover(media, {
                "with_genres": cat["genre_id"],
                "sort_by": "popularity.desc",
                "vote_count.gte": 20,
            }, page, offset)
        else:
            return jsonify({"metas": []}), 404
    except Exception as e:
        return jsonify({"metas": [], "error": str(e)}), 502

    items = [(it, to_meta(it, ctype, media)) for it in results]
    items = [(it, m) for it, m in items if m.get("poster")]
    with ThreadPoolExecutor(max_workers=10) as ex:
        provs = list(ex.map(lambda it: get_providers(media, it.get("id")),
                            [it for it, _ in items]))
    metas = []
    for (_, m), p in zip(items, provs):
        if p:
            desc = m.get("description") or ""
            m["description"] = (desc + "\n\n📺 Streaming: " + ", ".join(p)).strip()
        metas.append(m)
    return jsonify({"metas": metas})


@app.route("/meta/<mtype>/<mid>.json")
def meta(mtype, mid):
    if not mid.startswith("ott:"):
        # Catalog IDs (e.g. from "see all") -> return a catalog-level meta
        # instead of 404 so clients don't hit "no addon provides meta".
        cat = next((c for c in CATALOGS if c["id"] == mid), None)
        if cat:
            return jsonify({"meta": {
                "id": cat["id"],
                "type": cat["type"],
                "name": cat["name"],
                "description": f"Browse all titles in {cat['name']}.",
            }})
        return jsonify({"meta": {}}), 404
    slug = mid[4:]
    prov = next((p for p in OTT_PROVIDERS if p[3] == slug), None)
    if not prov:
        return jsonify({"meta": {}}), 404
    name, pid, region, _slug = prov
    base = tile_base()
    try:
        data = tmdb_get("/discover/movie", {
            "with_watch_providers": pid,
            "watch_region": region,
            "sort_by": "popularity.desc",
            "page": 1,
            "include_adult": "false",
            "vote_count.gte": 10,
        })
        titles = [f"\u2022 {(it.get('title') or it.get('name'))} ({(it.get('release_date') or '')[:4]})"
                  for it in data.get("results", [])[:10]]
    except Exception:
        titles = []
    where = "India" if region == "IN" else region
    desc = f"Top titles streaming on {name} ({where}):\n\n" + "\n".join(titles)
    return jsonify({"meta": {
        "id": mid,
        "type": "movie",
        "name": name,
        "poster": f"{base}/static/ott/{slug}.png",
        "background": f"{base}/static/ott/{slug}.png",
        "description": desc,
    }})


@app.route("/")
def index():
    html = """<h2>Nandus addon is running ✅</h2>
    <p>Install in Stremio / Nuvio:<br><code>{}/manifest.json</code></p>""".format(
        request.host_url.rstrip("/")
    )
    return Response(html, mimetype="text/html")


# ---------------- Msone addon (separate addon, same hosting) ----------------
# Static snapshot of malayalamsubtitles.org (the site blocks datacenter IPs, so
# live scraping from the server is not possible). Titles pre-resolved to TMDB
# in mzone_data.json: "sections" (curated homepage rows) + "archive" (full
# /releases/ crawl). Catalogs are built dynamically: curated rows, All
# Releases, Movies/Series, one row per language, one row per genre.
TMDB_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
    878: "Science Fiction", 10770: "TV Movie", 53: "Thriller", 10752: "War",
    37: "Western", 10759: "Action & Adventure", 10762: "Kids", 10763: "News",
    10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk",
    10768: "War & Politics",
}
_mzone_data = None
_mzone_metas = {}
_mzone_defs = None


def _mzone_lang_of(it):
    m = re.search(r"/languages/([^/]+)/", it.get("post_url", ""))
    return m.group(1) if m else "other"


def mzone_catalog_defs():
    """Full catalog list, built from the archive (cached)."""
    global _mzone_defs
    if _mzone_defs is not None:
        return _mzone_defs
    data = mzone_load()
    arch = data.get("archive", [])
    cats = [
        {"id": "mzone_new", "type": "movie", "name": "Msone: New Releases",
         "kind": "section", "section": "New Releases"},
        {"id": "mzone_trending", "type": "movie", "name": "Msone: Trending Today",
         "kind": "section", "section": "Trending Today"},
        {"id": "mzone_random", "type": "movie", "name": "Msone: Random Picks",
         "kind": "section", "section": "Random Picks"},
        {"id": "mzone_all", "type": "movie", "name": "Msone: All Releases",
         "kind": "all"},
        {"id": "mzone_movies", "type": "movie", "name": "Msone: Movies",
         "kind": "media", "media": "movie"},
        {"id": "mzone_series", "type": "series", "name": "Msone: Series",
         "kind": "media", "media": "tv"},
        {"id": "mzone_docs", "type": "movie", "name": "Msone: Documentaries",
         "kind": "genre", "genre_id": 99},
    ]
    lc = Counter(_mzone_lang_of(it) for it in arch)
    gc = Counter(g for it in arch for g in it.get("genre_ids", []))
    for gid, _ in gc.most_common():
        cats.append({"id": f"mzone_genre_{gid}", "type": "movie",
                     "name": f"Msone: {TMDB_GENRES.get(gid, f'Genre {gid}')}",
                     "kind": "genre", "genre_id": gid})
    for lang, _ in lc.most_common():
        cats.append({"id": f"mzone_lang_{lang}", "type": "movie",
                     "name": f"Msone: {lang.replace('-', ' ').title()}",
                     "kind": "lang", "lang": lang})
    _mzone_defs = cats
    return cats


def mzone_load():
    global _mzone_data
    if _mzone_data is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mzone_data.json")
        with open(p, encoding="utf-8") as f:
            _mzone_data = json.load(f)
    return _mzone_data


def _mzone_items_for_cat(cat):
    """Raw archive/section items for a catalog definition (no meta building)."""
    data = mzone_load()
    kind = cat["kind"]
    if kind == "section":
        return data["sections"].get(cat["section"], [])
    elif kind == "all":
        return data.get("archive", [])
    elif kind == "media":
        return [it for it in data.get("archive", []) if it["media"] == cat["media"]]
    elif kind == "lang":
        return [it for it in data.get("archive", [])
                if _mzone_lang_of(it) == cat["lang"]]
    elif kind == "genre":
        return [it for it in data.get("archive", [])
                if cat["genre_id"] in it.get("genre_ids", [])]
    return []


def mzone_metas(cid):
    if cid in _mzone_metas:
        return _mzone_metas[cid]
    cat = next(c for c in mzone_catalog_defs() if c["id"] == cid)
    items = _mzone_items_for_cat(cat)
    kind = cat["kind"]
    metas = []
    for it in items:
        disp = it["name"] + (f" / {it['name_ml']}" if it.get("name_ml") else "")
        # Prefer IMDb ID (tt...) for stream addon compatibility; fallback to tmdb:
        cid = it.get("imdb_id") or f"tmdb:{it['id']}"
        m = {
            "id": cid,
            "type": it["type"],
            "name": disp,
            "poster": f"{POSTER}{it['poster_path']}" if it.get("poster_path") else None,
            "background": f"{BG}{it['backdrop_path']}" if it.get("backdrop_path") else None,
            "description": ((it.get("overview") or "")
                            + f"\n\n\U0001F4DD Malayalam subtitles: Msone\n{it['post_url']}").strip(),
            "releaseInfo": it.get("year") or "",
            "genres": genre_names(it["media"], it.get("genre_ids")),
        }
        metas.append(m)
    metas = [m for m in metas if m.get("poster")]
    # streaming providers only for the small curated rows (archive rows would
    # need thousands of TMDB calls)
    if kind == "section":
        try:
            with ThreadPoolExecutor(max_workers=10) as ex:
                provs = list(ex.map(lambda it: get_providers(it["media"], it["id"]), items))
            for m, p in zip(metas, provs):
                if p:
                    m["description"] = (m["description"] + "\n\n\U0001F4FA Streaming: "
                                        + ", ".join(p)).strip()
        except Exception:
            pass
    _mzone_metas[cid] = metas
    return metas


# ---------------- Msone meta ----------------
# Self-contained meta for our tmdb: IDs, so detail/see-all flows work even
# without relying on other meta addons.
_mzone_by_id = None


def mzone_by_id():
    global _mzone_by_id
    if _mzone_by_id is None:
        data = mzone_load()
        idx = {}
        for it in data.get("archive", []):
            idx[str(it["id"])] = it
            if it.get("imdb_id"):
                idx[it["imdb_id"]] = it
        for sec_items in data.get("sections", {}).values():
            for it in sec_items:
                idx.setdefault(str(it["id"]), it)
                if it.get("imdb_id"):
                    idx.setdefault(it["imdb_id"], it)
        _mzone_by_id = idx
    return _mzone_by_id


@app.route("/mzone/meta/<mtype>/<mid>.json")
def mzone_meta(mtype, mid):
    # If mid is one of our catalog IDs, return a catalog-level meta so
    # clients that resolve "see all" via meta don't hit a dead end.
    cat = next((c for c in mzone_catalog_defs() if c["id"] == mid), None)
    if cat:
        items = _mzone_items_for_cat(cat)
        first = items[0] if items else None
        top = "\n".join(
            f"\u2022 {it['name']}" + (f" / {it['name_ml']}" if it.get("name_ml") else "")
            + (f" ({it['year']})" if it.get("year") else "")
            for it in items[:8]
        )
        meta = {
            "id": cat["id"],
            "type": cat["type"],
            "name": cat["name"],
            "poster": f"{POSTER}{first['poster_path']}" if first and first.get("poster_path") else None,
            "background": f"{BG}{first['backdrop_path']}" if first and first.get("backdrop_path") else None,
            "description": (f"{len(items)} titles in {cat['name']}.\n\nTop titles:\n{top}"
                            f"\n\n\U0001F4DD Malayalam subtitles: Msone").strip(),
        }
        meta = {k: v for k, v in meta.items() if v}
        return jsonify({"meta": meta})
    # mid can be "tmdb:12345", "tt1234567", or a catalog ID
    # anything else -> graceful empty (not 404)
    if mid.startswith("tt"):
        it = mzone_by_id().get(mid)
        tid = mid
    else:
        tid = mid.split(":", 1)[1] if ":" in mid else mid
        it = mzone_by_id().get(tid) if tid.isdigit() else None
        tid = f"tmdb:{tid}" if it else mid
    if not it:
        return jsonify({"meta": {}})
    disp = it["name"] + (f" / {it['name_ml']}" if it.get("name_ml") else "")
    # Use the requested ID format so stream addons get what they expect
    out_id = mid if (mid.startswith("tt") or mid.startswith("tmdb:")) else (it.get("imdb_id") or f"tmdb:{it['id']}")
    meta = {
        "id": out_id,
        "type": "series" if it["type"] == "series" else "movie",
        "name": disp,
        "poster": f"{POSTER}{it['poster_path']}" if it.get("poster_path") else None,
        "background": f"{BG}{it['backdrop_path']}" if it.get("backdrop_path") else None,
        "description": ((it.get("overview") or "")
                        + f"\n\n\U0001F4DD Malayalam subtitles: Msone\n{it['post_url']}").strip(),
        "releaseInfo": it.get("year") or "",
        "genres": genre_names(it["media"], it.get("genre_ids")),
    }
    meta = {k: v for k, v in meta.items() if v}
    return jsonify({"meta": meta})


@app.route("/mzone/manifest.json")
def mzone_manifest():
    return jsonify({
        "id": "com.mzone.catalog",
        "version": "1.2.7",
        "name": "Msone by Nandu10",
        "description": "Msone — world cinema with Malayalam subtitles (TMDB metadata)",
        "logo": f"{request.url_root.rstrip('/')}/static/msone-logo.png",
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "tmdb:", "mzone_"],
        "resources": ["catalog", "meta"],
        "catalogs": [
            {"type": c["type"], "id": c["id"], "name": c["name"],
             "extra": [{"name": "skip", "isRequired": False}]}
            for c in mzone_catalog_defs()
        ],
    })


@app.route("/mzone/catalog/<ctype>/<cid>.json")
@app.route("/mzone/catalog/<ctype>/<cid>/skip=<int:skip>.json")
def mzone_catalog(ctype, cid, skip=0):
    if not TMDB_API_KEY:
        return jsonify({"metas": [], "error": "TMDB_API_KEY not configured"}), 500
    cat = next((c for c in mzone_catalog_defs() if c["id"] == cid), None)
    if not cat:
        return jsonify({"metas": []}), 404
    try:
        metas = mzone_metas(cid)
    except Exception as e:
        return jsonify({"metas": [], "error": str(e)}), 502
    if "skip" not in request.view_args:
        try:
            skip = int(request.args.get("skip", "0"))
        except ValueError:
            skip = 0
    return jsonify({"metas": metas[skip:skip + 20]})


# ================================================================
# Movie Mirror addons (catalog + subtitles)
# Merged from mm_catalog/app.py
# ================================================================

MM_SITE = "https://moviemirrorsubtitles.com"
MM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MM_VERSION = "1.0.0"
MM_SUB_VERSION = "1.0.0"
SUB_VERSION = MM_SUB_VERSION

# Note: mm code uses SITE and UA - defining them here
SITE = MM_SITE
UA = MM_UA

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
            if wait > 0 and MM_SITE in url:
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
                if MM_SITE in url:
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
