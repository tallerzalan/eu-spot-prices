# European spot prices

Interactive Streamlit dashboard of day-ahead electricity prices for 46 European bidding zones,
fed by the [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) through
[entsoe-py](https://github.com/EnergieID/entsoe-py).

- **Map tab** - click a zone (shift-click for several) to get its hourly prices in a table, plus
  average / min / max and cheapest / priciest hour. The map can show the daily average or a single hour.
- **Matrix tab** - every zone x every hour for the chosen day, grouped into CWE, Nordics, Baltics,
  Eastern Europe, Balkan, Italy and Iberia, with a regional summary.

## How it works

```
GitHub Actions (hourly 13:30-17:30 market time)
   -> scripts/fetch_prices.py  -> data/prices.csv  (committed to the repo)
                                        |
                       Streamlit app (app.py) reads the CSV
```

- ENTSO-E publishes next-day prices around 13:00 CET, but some zones appear later. The workflow
  therefore runs several times in the afternoon and commits only when data changed.
- The CSV holds the last 60 days of hourly prices (`zone, time (UTC), price_eur_mwh`).
- Times in the app are shown in market time (CET/CEST). 15-minute prices are averaged to hours.
- `zones.py` is the single list of zones, names and market regions.
- `data/zones.geojson` was built with `scripts/build_geo.py` from Natural Earth (public domain).
  Sweden/Norway/Denmark/Italy zones are grouped from county/region polygons, so their borders
  are approximate.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

To refresh the data yourself you need an ENTSO-E API token (register on the Transparency Platform,
then email transparency@entsoe.eu to request "Restful API access"):

```bash
pip install -r requirements-fetch.txt
set ENTSOE_API_KEY=your-token      # macOS/Linux: export ENTSOE_API_KEY=your-token
python scripts/fetch_prices.py
```

## Deploy

1. Push this repo to GitHub.
2. Repo **Settings -> Secrets and variables -> Actions -> New repository secret**:
   name `ENTSOE_API_KEY`, value = your token.
3. Repo **Settings -> Actions -> General -> Workflow permissions**: *Read and write permissions*.
4. **Actions** tab -> *Update spot prices* -> *Run workflow* once to test.
5. On [share.streamlit.io](https://share.streamlit.io) create an app from the repo with main file `app.py`.
   It redeploys automatically whenever the workflow commits new prices.

Never commit the token. It only lives in the GitHub secret / your environment.
