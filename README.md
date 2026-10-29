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

- **Regulation tab** - ENTSO-E does not publish activated balancing energy volumes through its public API, so
  this tab shows the two things it does publish: the *balancing state* (the TSO's minute-by-minute need for up- or
  downregulation, MW) and the *available aggregated bids* for aFRR and mFRR (MW). Per zone: period x day heatmap,
  up/down charts over time and by time of day, products table, bids, and a comparison of all zones.

- **Intraday auctions tab** - IDA1 (15:00 D-1), IDA2 (22:00 D-1) and IDA3 (10:00 D) prices next to day-ahead, and
  the spreads between any two of them (later minus earlier; green = later auction higher). ENTSO-E only publishes
  these prices for Spain; more zones can be added when a source exists (`pipeline/ida.py`).

**Granularity** - the toggle at the top of the sidebar switches the whole dashboard between hourly means and
15-minute values (map, tables, matrix and imbalance tab). 15-minute data is kept for 30 days in
`data/prices_15min.csv` and `data/imbalance_15min.csv`; the first workflow runs after this feature backfill it.

## How it works

```
GitHub Actions (three workflows)            pipeline/  (python -m pipeline <dataset>)
  Update spot prices  13:30-17:30 hourly ->   spot        -> data/prices_15min.csv, prices.csv
  Update imbalance    every hour         ->   imbalance   -> data/imbalance_15min.csv, imbalance.csv, ..._currency.json
  Update regulation   every hour         ->   regulation  -> data/regulation_15min.csv, regulation_status.json
  Update intraday auctions hourly        ->   ida         -> data/ida_15min.csv
  (spot also)                            ->   fx          -> data/fx.csv (ECB reference rates)
                                                 |
                              Streamlit app (app.py) reads the CSV files
```

- `pipeline/base.py` holds what all datasets share (incremental fetch with a one-time backfill, parallel zones,
  time budget, hourly derivation, merge and retention); each dataset implements `fetch_zone()` only.
- ENTSO-E publishes next-day prices around 13:00 CET, but some zones appear later. The spot workflow therefore runs
  hourly in the afternoon and commits only when data changed.
- Prices are stored at 15-minute resolution (30 days) and as hourly means (60 days spot, 45 days imbalance).
  Imbalance volumes are sums, positive = surplus, negative = deficit. ENTSO-E quotes imbalance prices in each
  country's own currency (CZK, PLN, RON); the app converts them to EUR with the ECB daily reference rate. Nothing is
  dropped as an outlier. Colours: green = bull (premium above spot, upregulation), red = bear.
- Times in the app are shown in market time (CET/CEST). `zones.py` is the single list of zones and regions.
- `data/zones.geojson` was built with `scripts/build_geo.py` from Natural Earth (public domain). Sweden/Norway/
  Denmark/Italy zones are grouped from county/region polygons, so their borders are approximate.

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
python -m pipeline spot
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
