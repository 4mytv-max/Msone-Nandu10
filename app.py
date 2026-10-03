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

import os
import re
import time
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


# ---------------- MZone addon (separate addon, same hosting) ----------------
# Curates malayalamsubtitles.org homepage sections (world cinema with
# Malayalam subtitles) and maps each title to TMDB metadata.
MZONE_CATALOGS = [
    {"id": "mzone_new", "type": "movie", "name": "MZone: New Releases",
     "section": "New Releases"},
    {"id": "mzone_trending", "type": "movie", "name": "MZone: Trending Today",
     "section": "Trending Today"},
    {"id": "mzone_random", "type": "movie", "name": "MZone: Random Picks",
     "section": "Random Picks"},
]
MZONE_TTL = 12 * 3600
_mzone_cache = {"ts": 0.0, "data": {}}
_mzone_resolve_cache = {}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def mzone_parse(html):
    """Extract {section: [(title_en, year, post_url)]} from MZone homepage."""
    heads = [(m.start(), re.sub(r"<[^>]+>", "", m.group(1)).strip())
             for m in re.finditer(r"<h[1-4][^>]*>(.*?)</h[1-4]>", html, re.S)]
    valid = {c["section"] for c in MZONE_CATALOGS}
    slides = [(m.start(), m.group(1), re.sub(r"\s+", " ", m.group(2)).strip())
              for m in re.finditer(
                  r'<div class="slide-title[^"]*">\s*<a href="([^"]+)">\s*(.*?)\s*</a>',
                  html, re.S)]
    out = {}
    for spos, url, raw in slides:
        sec = None
        for hpos, hname in heads:
            if hpos < spos and hname in valid:
                sec = hname
        if not sec:
            continue
        m = re.match(r"^(.*?)\s*/.*?\(?\b(19\d\d|20\d\d)\)?\s*$", raw)
        if m:
            title_en, year = m.group(1).strip(), m.group(2)
        else:
            title_en, year = raw.split("/")[0].strip(), ""
        title_en = re.sub(r"\s+[Ss]eason\s*\d+\s*$", "", title_en)
        title_en = re.sub(r"\s+S0?\d+\s*$", "", title_en)
        if title_en:
            out.setdefault(sec, []).append((title_en, year, url))
    return out


def mzone_resolve(title_en, year):
    """Map an MZone title to (tmdb_item, 'movie'|'tv'), cached."""
    key = f"{title_en}|{year}"
    if key in _mzone_resolve_cache:
        return _mzone_resolve_cache[key]
    norm_q = _norm(title_en)
    exact = close = None
    for media in ("movie", "tv"):
        try:
            d = tmdb_get(f"/search/{media}", {"query": title_en, "include_adult": "false"})
        except Exception:
            continue
        for r in d.get("results", [])[:6]:
            t = r.get("title") or r.get("name") or ""
            n = _norm(t)
            if not n:
                continue
            dy = (r.get("release_date") or r.get("first_air_date") or "")[:4]
            year_ok = (not year) or (dy == year)
            if n == norm_q and year_ok and not exact:
                exact = (r, media)
            elif (n == norm_q or norm_q in n or n in norm_q) and not close:
                close = (r, media)
        if exact:
            break
    res = exact or close or (None, None)
    _mzone_resolve_cache[key] = res
    return res


def mzone_build():
    now = time.time()
    if now - _mzone_cache["ts"] < MZONE_TTL and _mzone_cache["data"]:
        return _mzone_cache["data"]
    data = {}
    try:
        html = requests.get(
            "https://malayalamsubtitles.org/",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=25).text
        sections = mzone_parse(html)
        for cat in MZONE_CATALOGS:
            metas = []
            raws = []
            for title_en, year, post_url in sections.get(cat["section"], [])[:20]:
                r, media = mzone_resolve(title_en, year)
                if not r:
                    continue
                ctype = "movie" if media == "movie" else "series"
                m = to_meta(r, ctype, media)
                desc = m.get("description") or ""
                m["description"] = (
                    desc + f"\n\n📝 Malayalam subtitles: MZone\n{post_url}").strip()
                metas.append(m)
                raws.append((r, media))
            with ThreadPoolExecutor(max_workers=10) as ex:
                provs = list(ex.map(lambda rm: get_providers(rm[1], rm[0].get("id")), raws))
            for m, p in zip(metas, provs):
                if p:
                    m["description"] = (
                        m["description"] + "\n\n📺 Streaming: " + ", ".join(p)).strip()
            data[cat["id"]] = metas
    except Exception as e:
        data = {"error": str(e)}
    _mzone_cache["ts"] = now
    _mzone_cache["data"] = data
    return data


@app.route("/mzone/manifest.json")
def mzone_manifest():
    return jsonify({
        "id": "com.mzone.catalog",
        "version": "1.0.0",
        "name": "MZone",
        "description": "MZone — world cinema with Malayalam subtitles (TMDB metadata)",
        "types": ["movie", "series"],
        "idPrefixes": ["tmdb:"],
        "resources": ["catalog"],
        "catalogs": [
            {"type": c["type"], "id": c["id"], "name": c["name"],
             "extra": [{"name": "skip", "isRequired": False}]}
            for c in MZONE_CATALOGS
        ],
    })


@app.route("/mzone/catalog/<ctype>/<cid>.json")
def mzone_catalog(ctype, cid):
    if not TMDB_API_KEY:
        return jsonify({"metas": [], "error": "TMDB_API_KEY not configured"}), 500
    cat = next((c for c in MZONE_CATALOGS if c["id"] == cid), None)
    if not cat:
        return jsonify({"metas": []}), 404
    data = mzone_build()
    if "error" in data:
        return jsonify({"metas": [], "error": data["error"]}), 502
    try:
        skip = int(request.args.get("skip", "0"))
    except ValueError:
        skip = 0
    return jsonify({"metas": data.get(cid, [])[skip:skip + 20]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
