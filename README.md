# European spot prices

Interactive Streamlit dashboard of day-ahead electricity prices for 46 European bidding zones,
fed by the [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) through
[entsoe-py](https://github.com/EnergieID/entsoe-py).

- **Map tab** - a full-page map of the zones coloured by a power-market product: Base (24 h average), Peak
  (08-20), Off-peak 1 (00-08), Off-peak 2 (20-24), Off-peak, Morning shoulder (06-09), Evening shoulder
  (17-20), or a single hour. Click a zone to add it to the selection, click again to remove it. Underneath:
  those products per selected zone, an hourly table and a chart. The block definitions live in `PRODUCTS`
  at the top of `app.py`.
- **Matrix tab** - hours 1-24 down, zones across, grouped into CWE (incl. Ireland), Nordics, Baltics,
  Eastern Europe, Balkan, Italy and Iberia, with the same products per region and per zone.
- **Imbalance tab** - pick a zone: hour x day heatmap of the imbalance price, premium or volume (newest day
  first, up to 30 days), spot vs imbalance price over time and by hour of day, the premium by product,
  zone comparison and a summary. The *imbalance price* is the price in the direction the system was out of
  balance (short price when in deficit, long price when in surplus); *premium* = imbalance price - spot.

## How it works

```
GitHub Actions (hourly 13:30-17:30 market time)
   -> scripts/fetch_prices.py     -> data/prices.csv     (committed to the repo)
   -> scripts/fetch_imbalance.py  -> data/imbalance.csv  + imbalance_currency.json
                                        |
                       Streamlit app (app.py) reads the CSV
```

- ENTSO-E publishes next-day prices around 13:00 CET, but some zones appear later. The workflow
  therefore runs several times in the afternoon and commits only when data changed.
- `prices.csv` holds the last 60 days of hourly spot prices (`zone, time (UTC), price_eur_mwh`).
- `imbalance.csv` holds 45 days of hourly imbalance data (`zone, time, price_long, price_short, volume_mwh`).
  Prices are means, volumes are sums; positive volume = surplus, negative = deficit. ENTSO-E quotes imbalance
  prices in each country's own currency, so the currency per zone is stored and non-EUR zones are not compared
  with spot. Not every zone publishes imbalance data.
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
