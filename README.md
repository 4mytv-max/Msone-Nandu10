# Nandus — Stremio addon

Latest **Malayalam**, **Tamil** & **Korean** movies + **Korean series** catalogs for
Stremio / Nuvio, powered by TMDB. Catalog-only (no streams) — pair it with your
existing stream addons (TorBox etc.) in Nuvio.

Catalogs:
- `ml_latest` — Latest Malayalam movies
- `ta_latest` — Latest Tamil movies
- `ko_latest` — Latest Korean movies
- `ko_series` — Korean series

## 1. Get a free TMDB API key

1. Sign up free at https://www.themoviedb.org and verify your email.
2. Log in → click your avatar (top right) → **Settings** → **API** (left sidebar).
3. Click **Create** → choose **Developer** → accept the terms.
4. Fill the form (Application Name: `Nandus`, URL: anything) → Submit.
5. Copy the **API Key (v3 auth)**.

## 2. Deploy free on Render

1. Put `app.py` + `requirements.txt` in a GitHub repo (upload via github.com web UI).
2. Sign up free at https://render.com → **New +** → **Web Service** →
   connect the GitHub repo.
3. Settings: Build Command `pip install -r requirements.txt`,
   Start Command `gunicorn app:app`.
   (Render auto-detects Python; set both if asked.)
4. **Environment variable**: add `TMDB_API_KEY` = your key from step 1.
5. Deploy → you get a URL like `https://mtk-catalog.onrender.com`.

> Note: Render's free tier sleeps after inactivity — first catalog load may take
> ~30 seconds to wake up. After that it's fast.

## 3. Install in Stremio / Nuvio

Addons → Add addon → paste: `https://<your-render-url>/manifest.json` → Install.

Open the app → Board/Discover → you'll see the 4 new catalogs.
Tap any title → streams come from your existing installed stream addons.

## Local test

```bash
export TMDB_API_KEY=your_key_here
pip install -r requirements.txt
python app.py
# open http://localhost:5000/manifest.json
```
