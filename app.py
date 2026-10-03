"""
MTK Catalog - Stremio catalog addon
Latest Malayalam, Tamil & Korean movies + Korean series, powered by TMDB.

Deploy: set TMDB_API_KEY env var, run with gunicorn (see README).
Install in Stremio/Nuvio: <your-url>/manifest.json
"""

import os
from datetime import date

import requests
from flask import Flask, jsonify, request, Response

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
TMDB = "https://api.themoviedb.org/3"
POSTER = "https://image.tmdb.org/t/p/w500"
BG = "https://image.tmdb.org/t/p/w1280"

app = Flask(__name__)

CATALOGS = [
    {"id": "ml_latest", "type": "movie", "name": "Latest Malayalam",
     "media": "movie", "lang": "ml"},
    {"id": "ta_latest", "type": "movie", "name": "Latest Tamil",
     "media": "movie", "lang": "ta"},
    {"id": "ko_latest", "type": "movie", "name": "Latest Korean",
     "media": "movie", "lang": "ko"},
    {"id": "ko_series", "type": "series", "name": "Korean Series",
     "media": "tv", "lang": "ko"},
]

_genre_cache = {}


def tmdb_get(path, params):
    params = dict(params or {})
    params["api_key"] = TMDB_API_KEY
    r = requests.get(f"{TMDB}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


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
        "version": "1.0.0",
        "name": "Nandus",
        "description": "Latest Malayalam, Tamil & Korean movies and Korean series (TMDB)",
        "types": ["movie", "series"],
        "idPrefixes": ["tmdb:"],
        "resources": ["catalog"],
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


@app.route("/catalog/<ctype>/<cid>.json")
@app.route("/catalog/<ctype>/<cid>/<extra>.json")
def catalog(ctype, cid, extra=""):
    if not TMDB_API_KEY:
        return jsonify({"metas": [], "error": "TMDB_API_KEY not configured"}), 500

    cat = next((c for c in CATALOGS if c["id"] == cid and c["type"] == ctype), None)
    if not cat:
        return jsonify({"metas": []}), 404

    # skip -> page mapping (TMDB returns 20 items/page)
    try:
        skip = int(request.args.get("skip", "0"))
    except ValueError:
        skip = 0
    page = skip // 20 + 1
    offset = skip % 20

    params = {
        "with_original_language": cat["lang"],
        "sort_by": "primary_release_date.desc" if cat["media"] == "movie" else "first_air_date.desc",
        "page": page,
        "include_adult": "false",
        "vote_count.gte": 3,
    }
    if cat["media"] == "movie":
        params["primary_release_date.lte"] = date.today().isoformat()
        params["primary_release_date.gte"] = "2023-01-01"
    else:
        params["first_air_date.lte"] = date.today().isoformat()
        params["first_air_date.gte"] = "2023-01-01"

    try:
        data = tmdb_get(f"/discover/{cat['media']}", params)
    except Exception as e:
        return jsonify({"metas": [], "error": str(e)}), 502

    results = data.get("results", [])[offset:]
    metas = [to_meta(it, ctype, cat["media"]) for it in results]
    # drop items without poster for a clean shelf look
    metas = [m for m in metas if m.get("poster")]
    return jsonify({"metas": metas})


@app.route("/")
def index():
    html = """<h2>Nandus addon is running ✅</h2>
    <p>Install in Stremio / Nuvio:<br><code>{}/manifest.json</code></p>""".format(
        request.host_url.rstrip("/")
    )
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
