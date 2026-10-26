"""Fetch imbalance prices and volumes for all dashboard zones into data/imbalance.csv.

Columns: zone, time (UTC, hourly), price_long, price_short, volume_mwh
  - price_long / price_short: imbalance price for parties that are long (surplus) / short (deficit),
    hourly mean of the published values. Single-price systems publish one price; it is stored
    in both columns.
  - volume_mwh: net system imbalance, hourly sum. ENTSO-E sign convention: positive = surplus
    (system long), negative = deficit (system short).
Imbalance prices are published in each country's own currency; the currency per zone is kept in
data/imbalance_currency.json. Not every zone publishes imbalance data - those simply have no rows.

Incremental, like fetch_prices.py: re-fetches the last few days and merges.

Usage:  ENTSOE_API_KEY=... python scripts/fetch_imbalance.py [--history-days 35]
"""
import argparse
import io
import json
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from entsoe import EntsoePandasClient, EntsoeRawClient
from entsoe.exceptions import NoMatchingDataError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from zones import ZONE_CODES  # noqa: E402

CSV = ROOT / "data" / "imbalance.csv"
CURRENCY_JSON = ROOT / "data" / "imbalance_currency.json"
MARKET_TZ = "Europe/Brussels"
KEEP_DAYS = 45
OVERLAP_DAYS = 2
TIME_BUDGET_S = 12 * 60
WORKERS = 6

parser = argparse.ArgumentParser()
parser.add_argument("--history-days", type=int, default=35, help="how far back to start for zones without stored data")
args = parser.parse_args()

API_KEY = os.environ["ENTSOE_API_KEY"]
started = time.monotonic()

old = pd.DataFrame(columns=["zone", "time", "price_long", "price_short", "volume_mwh"])
if CSV.exists():
    old = pd.read_csv(CSV, parse_dates=["time"])
    old["time"] = pd.to_datetime(old["time"], utc=True)
currency = json.loads(CURRENCY_JSON.read_text()) if CURRENCY_JSON.exists() else {}

today = pd.Timestamp.now(tz=MARKET_TZ).normalize()
floor = today - pd.Timedelta(days=KEEP_DAYS)
end = today + pd.Timedelta(days=1)  # exclusive: through the end of today

latest = old.groupby("zone")["time"].max().reindex(ZONE_CODES)
order = sorted(ZONE_CODES, key=lambda z: latest[z] if pd.notna(latest[z]) else pd.Timestamp(0, tz="UTC"))


def start_for(zone):
    if pd.isna(latest[zone]):
        return today - pd.Timedelta(days=args.history_days)
    return max(latest[zone].tz_convert(MARKET_TZ).normalize() - pd.Timedelta(days=OVERLAP_DAYS), floor)


def call(fn, *a, **kw):
    """Run an ENTSO-E query with retries. Returns None when nothing is published."""
    for attempt in range(3):
        try:
            return fn(*a, **kw)
        except NoMatchingDataError:
            return None
        except Exception as exc:  # network / rate limit / 5xx
            print(f"  {a[0]}: {type(exc).__name__} (attempt {attempt + 1})", file=sys.stderr, flush=True)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("retries exhausted")


def detect_currency(zone):
    """ENTSO-E quotes imbalance prices in local currency; pull it from the raw XML."""
    raw = EntsoeRawClient(api_key=API_KEY, timeout=60, retry_count=2, retry_delay=5)
    blob = call(raw.query_imbalance_prices, zone, start=today - pd.Timedelta(days=3), end=today)
    if not blob:
        return None
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            m = re.search(rb"<currency_Unit\.name>\s*([A-Za-z]{3})\s*<", zf.read(name), re.IGNORECASE)
            if m:
                return m.group(1).decode().upper()
    return None


def fetch_zone(zone):
    """-> (zone, status, hourly DataFrame, currency). status: ok | no_data | error | skipped"""
    if time.monotonic() - started > TIME_BUDGET_S:
        return zone, "skipped", None, None
    client = EntsoePandasClient(api_key=API_KEY, timeout=60, retry_count=2, retry_delay=5)
    try:
        prices = call(client.query_imbalance_prices, zone, start=start_for(zone), end=end)
        volumes = call(client.query_imbalance_volumes, zone, start=start_for(zone), end=end)
        parts = {}
        if prices is not None and len(prices):
            prices = prices.tz_convert("UTC")
            long_ = prices["Long"] if "Long" in prices else None
            short = prices["Short"] if "Short" in prices else None
            if long_ is None and short is None:  # single-price system: one column, used for both
                long_ = short = prices.iloc[:, 0]
            parts["price_long"] = (long_ if long_ is not None else short).resample("h").mean()
            parts["price_short"] = (short if short is not None else long_).resample("h").mean()
        if volumes is not None and len(volumes):
            vol = volumes["Imbalance Volume"] if isinstance(volumes, pd.DataFrame) else volumes
            parts["volume_mwh"] = vol.tz_convert("UTC").resample("h").sum(min_count=1)
        if not parts:
            return zone, "no_data", None, None
        hourly = pd.DataFrame(parts).dropna(how="all")
        cur = None
        if "price_long" in parts and zone not in currency:
            cur = detect_currency(zone)
        return zone, "ok", hourly, cur
    except Exception as exc:
        print(f"  {zone}: giving up ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
        return zone, "error", None, None


print(f"Fetching imbalance data for {len(order)} zones with {WORKERS} workers", flush=True)
frames, failed = [], {"no_data": [], "error": [], "skipped": []}
with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    for zone, status, hourly, cur in pool.map(fetch_zone, order):
        if status != "ok":
            failed[status].append(zone)
            continue
        if cur:
            currency[zone] = cur
        frames.append(hourly.rename_axis("time").reset_index().assign(zone=zone))

if not frames:
    print("No imbalance data returned for any zone.")
    for status, zones in failed.items():
        if zones:
            print(f"{status}: {', '.join(zones)}")
    sys.exit(0 if not failed["error"] else 1)

cols = ["zone", "time", "price_long", "price_short", "volume_mwh"]
new = pd.concat([f.reindex(columns=cols) for f in frames], ignore_index=True)
merged = pd.concat([old, new], ignore_index=True)
merged = merged.drop_duplicates(["zone", "time"], keep="last")
merged = merged[merged["time"] >= floor.tz_convert("UTC")].sort_values(["zone", "time"])
merged[["price_long", "price_short"]] = merged[["price_long", "price_short"]].round(2)
merged["volume_mwh"] = merged["volume_mwh"].round(1)

CSV.parent.mkdir(exist_ok=True)
merged.to_csv(CSV, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
CURRENCY_JSON.write_text(json.dumps(dict(sorted(currency.items())), indent=1))
print(f"{new['zone'].nunique()} zones with imbalance data, {len(merged)} rows stored in {CSV.name}")
print("currencies:", ", ".join(f"{z}={c}" for z, c in sorted(currency.items())))
for status, zones in failed.items():
    if zones:
        print(f"{status}: {', '.join(zones)}")
