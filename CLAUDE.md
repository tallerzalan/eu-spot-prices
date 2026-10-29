# European power dashboard

Streamlit dashboard (`app.py`) over ENTSO-E data: day-ahead spot prices, imbalance prices/volumes and regulation
(balancing state and bids) for 46 bidding zones. Data is fetched by GitHub Actions, committed as CSV to `data/`, and
the app (hosted on Streamlit Community Cloud) just reads the CSVs. Repo: github.com/tallerzalan/eu-spot-prices.

## Layout
- `app.py` - the whole dashboard (tabs: Map, Matrix by region, Imbalance, Regulation, Intraday auctions, Gas). Sidebar: granularity toggle
  (Hourly / 15-minute) and delivery day. Products (Base, Peak, ...) are defined once in `PRODUCTS`.
- `zones.py` - single list of zones, display names and market regions. Used by app and pipeline.
- `pipeline/` - the fetchers. `base.py` has the shared `Dataset` class (incremental fetch, parallel zones, time
  budget, hourly derivation, merge/retention, CSV writing); `spot.py`, `imbalance.py`, `regulation.py`, `ida.py`, `gas.py`, `fx.py`
  implement `fetch_zone()` only. Run: `python -m pipeline <spot|imbalance|regulation|ida|gas|fx> [--data-dir D] [--zones FR,NL]`.
- `data/` - CSVs written by the workflows (do not edit by hand). `zones.geojson` is built by `scripts/build_geo.py`.
- `.github/workflows/` - `update-spot` (attempts every 15 min, gated to 13:00-18:30 Brussels, skips when tomorrow is
  complete), `update-imbalance`, `update-regulation`, `update-ida`, `update-gas` (two attempts per hour). Each ends with
  `scripts/commit_data.sh` (rebase + retry on rejected push; several workflows push to main).
- `tests/` - offline tests (`pytest`), no API key needed.

## Conventions
- Times are stored in UTC and shown in Europe/Brussels. 15-minute is the stored resolution; hourly is derived
  (prices: mean, volumes: sum). Zones that publish coarser data are spread over the quarters (volumes split evenly).
- All prices shown in EUR. Imbalance prices come in local currency (CZK, PLN, RON) and are converted in `app.py`
  with the ECB rate of the day (`data/fx.csv`). Hungary quoted HUF until 30 Sep 2026 (`LEGACY_CURRENCY`).
- Colours: green = bull (above spot, upregulation, high price), red = bear (below spot, downregulation, low price).
  The spot map/matrix is the exception (red = expensive). Do not drop outliers; only cap colour scales.
- Row labels in 15-minute views are products: `H13Q2 12:15-12:30`. Hour 1 = 00:00-01:00.
- Match the surrounding style; keep comments short and explain why, not what.

## GitHub Actions reality
- Scheduled runs are heavily throttled: from the run history, `Update spot prices` fired about once a day instead of
  five times. So every workflow has several cron attempts per hour and exits/commits nothing when there is nothing
  new. Check `https://api.github.com/repos/tallerzalan/eu-spot-prices/actions/runs` (public, no auth) for what ran.
- For reliable timing an external scheduler (e.g. cron-job.org) would have to call the `workflow_dispatch` API with a
  fine-grained token; not set up.

## Gas data
- TTF (`TTF=F`, EUR/MWh, hourly bars) and NBP (`UKG=F`, pence/therm, daily bars only) come from the Yahoo chart API,
  no key. NBP is converted to EUR/MWh in the app with the ECB GBP rate. `NBP=F` on Yahoo is a different, stale series.
- Yahoo adds the live quote as an off-the-hour bar; `pipeline/gas.py` drops it so the hourly bars stay unique.

## ENTSO-E quirks (learned the hard way)
- entsoe-py 0.8.1. 15-minute day-ahead prices since 1 Oct 2025. Responses may be a ZIP of XML documents.
- `A83` (activated balancing energy, 17.1.E) and `A82` are REJECTED by the REST API for every parameter combination
  ("combination is not valid, or the requested data is not allowed to be fetched via this service", verified
  2026-10-29 over ~800 combinations). The official API collection only lists prices (A84), not volumes. So the
  Regulation tab uses `A86`+`B33` (current balancing state, 1-minute MW) and `A24` (aggregated bids) instead.
- `retry_count=0` on entsoe-py clients breaks its retry decorator ("exceptions must derive from BaseException").
- ENTSO-E limits: 400 requests/min per token; some data items cap documents per request (A15 procured capacity: 100).
- Intraday auction (IDA1-3) prices exist on ENTSO-E for Spain only (A44, contract A07, sequence 1-3). DE_LU/AT/DK_2
  expose "sequence 2" with A01 but its prices equal day-ahead, so it is not an IDA.
- Late publication: some zones appear hours after 13:00 CET, hence the hourly spot runs.

## Working here
- Windows. Keep the repo under a short path (`C:\dev\...`); long paths break git clones.
- Local token: `ENTSOE_API_KEY` env var (token file path is in the user's memory, never commit it).
  Test fetchers in a scratch dir: `python -m pipeline regulation --data-dir C:\dev\play\x --zones FR,NL`.
- Test the app headless with `streamlit.testing.v1.AppTest` and `SPOT_DATA_DIR` pointing at a data copy.
- Never push to `main` while a workflow run is in progress without expecting a rebase (the commit script handles it).
- Commit identity: use `git -c user.name=... -c user.email=...`; the workflows commit as github-actions[bot].
