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

import json
import os
import re
import time
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
        "version": "2.0.0",
        "name": "Nandus",
        "description": "Malayalam, Tamil & Korean catalogs + Streaming OTT row (TMDB)",
        "types": ["movie", "series"],
        "idPrefixes": ["tmdb:", "ott:"],
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
    ]
    lc = Counter(_mzone_lang_of(it) for it in arch)
    for lang, _ in lc.most_common():
        cats.append({"id": f"mzone_lang_{lang}", "type": "movie",
                     "name": f"Msone: {lang.replace('-', ' ').title()}",
                     "kind": "lang", "lang": lang})
    gc = Counter(g for it in arch for g in it.get("genre_ids", []))
    for gid, _ in gc.most_common():
        cats.append({"id": f"mzone_genre_{gid}", "type": "movie",
                     "name": f"Msone: {TMDB_GENRES.get(gid, f'Genre {gid}')}",
                     "kind": "genre", "genre_id": gid})
    _mzone_defs = cats
    return cats


def mzone_load():
    global _mzone_data
    if _mzone_data is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mzone_data.json")
        with open(p, encoding="utf-8") as f:
            _mzone_data = json.load(f)
    return _mzone_data


def mzone_metas(cid):
    if cid in _mzone_metas:
        return _mzone_metas[cid]
    cat = next(c for c in mzone_catalog_defs() if c["id"] == cid)
    data = mzone_load()
    kind = cat["kind"]
    if kind == "section":
        items = data["sections"].get(cat["section"], [])
    elif kind == "all":
        items = data.get("archive", [])
    elif kind == "media":
        items = [it for it in data.get("archive", []) if it["media"] == cat["media"]]
    elif kind == "lang":
        items = [it for it in data.get("archive", [])
                 if _mzone_lang_of(it) == cat["lang"]]
    elif kind == "genre":
        items = [it for it in data.get("archive", [])
                 if cat["genre_id"] in it.get("genre_ids", [])]
    else:
        items = []
    metas = []
    for it in items:
        disp = it["name"] + (f" / {it['name_ml']}" if it.get("name_ml") else "")
        m = {
            "id": f"tmdb:{it['id']}",
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


@app.route("/mzone/manifest.json")
def mzone_manifest():
    return jsonify({
        "id": "com.mzone.catalog",
        "version": "1.2.0",
        "name": "Msone by Nandu10",
        "description": "Msone — world cinema with Malayalam subtitles (TMDB metadata)",
        "types": ["movie", "series"],
        "idPrefixes": ["tmdb:"],
        "resources": ["catalog"],
        "catalogs": [
            {"type": c["type"], "id": c["id"], "name": c["name"],
             "extra": [{"name": "skip", "isRequired": False}]}
            for c in mzone_catalog_defs()
        ],
    })


@app.route("/mzone/catalog/<ctype>/<cid>.json")
def mzone_catalog(ctype, cid):
    if not TMDB_API_KEY:
        return jsonify({"metas": [], "error": "TMDB_API_KEY not configured"}), 500
    cat = next((c for c in mzone_catalog_defs() if c["id"] == cid), None)
    if not cat:
        return jsonify({"metas": []}), 404
    try:
        metas = mzone_metas(cid)
    except Exception as e:
        return jsonify({"metas": [], "error": str(e)}), 502
    try:
        skip = int(request.args.get("skip", "0"))
    except ValueError:
        skip = 0
    return jsonify({"metas": metas[skip:skip + 20]})



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
