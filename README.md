# Nandus + Msone — Stremio Catalog Addons

Two catalog-only Stremio addons (TMDB metadata, no streams) served from one app.

## Addons

### 1. Nandus (`/manifest.json`)
Curated Malayalam / Tamil / Korean catalogs + a Streaming OTT icon row.

Rows (in order):
1. **Streaming** — OTT icon tiles (JioHotstar, Zee5, Sony Liv, Sun NXT, Netflix,
   Prime Video, aha, Manorama Max, Discovery+, Lionsgate Play, MX Player,
   Tata Play, Rakuten Viki). Tap an icon for details.
2. Latest Malayalam Movies
3. Latest Tamil Movies
4. Latest Korean Movies
5. Latest Korean Series
6. Top Rated Korean Movies
7. Top Rated Korean Series
8. Latest Tamil Series
9. Latest Malayalam Series
10. Latest Tamil Dubbed Movies
11. Latest Tamil Dubbed Series
12. Tamil Dubbed Movies
13. Tamil Dubbed Series
14. K-Drama
15. Documentaries

Each title shows a `📺 Streaming: ...` line with its India subscription providers.

### 2. Msone (`/mzone/manifest.json`)
The complete malayalamsubtitles.org collection (എംസോൺ — ലോകസിനിമയുടെ മലയാള
ജാലകം): every post matched to TMDB metadata, cards show "English / മലയാളം"
titles like the site.

Rows (113 total):
- Msone: New Releases / Trending Today / Random Picks (curated)
- Msone: All Releases (full archive, ~3,300 titles)
- Msone: Movies / Msone: Series
- One row per language (Msone: English, Korean, Hindi, Japanese, French…) — 84
- One row per genre (Msone: Drama, Action, Horror, Comedy…) — 23

Each title notes `📝 Malayalam subtitles: Msone` with the post link.

Note: the site blocks datacenter IPs, so the catalog is served from a static
snapshot (`mzone_data.json`, generated 2026-10-04) instead of live scraping.

## Deploy (Render)

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Env var: `TMDB_API_KEY` = your TMDB API key (get one free at
  https://www.themoviedb.org/settings/api)
- Optional: `TILE_BASE` = public base URL override for OTT icon tiles
  (default: auto-detected from the request host).

## Install in Stremio / Nuvio

- Nandus: `https://<your-render-url>/manifest.json`
- Msone: `https://<your-render-url>/mzone/manifest.json`

Paste the manifest URL in Addons → "Install via URL".

## Local run

```bash
pip install -r requirements.txt
TMDB_API_KEY=your_key python app.py
```

## Notes

- Catalog-only: no streams are provided. Playback uses the user's own stream addons.
- Dubbed shelves are an approximation (popular non-native-language titles);
  they don't verify dubbed audio on any specific stream.
- MZone rows refresh every 12 hours from the site's homepage.
