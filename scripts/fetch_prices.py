"""Fetch day-ahead (spot) prices for all dashboard zones and merge them into data/prices.csv.

Incremental: re-fetches from a few days before the newest stored price through
tomorrow, so late corrections are picked up and the history keeps growing.
Prices are resampled to hourly means (some zones publish 15-minute prices).

Usage:  ENTSOE_API_KEY=... python scripts/fetch_prices.py [--history-days 14]
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from entsoe import EntsoePandasClient
from entsoe.exceptions import NoMatchingDataError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from zones import ZONE_CODES  # noqa: E402

CSV = ROOT / "data" / "prices.csv"
MARKET_TZ = "Europe/Brussels"  # ENTSO-E / market time
KEEP_DAYS = 60
OVERLAP_DAYS = 3
TIME_BUDGET_S = 12 * 60  # stop starting new zones after this; whatever arrived is still saved
WORKERS = 6  # ENTSO-E allows 400 requests/min per token

parser = argparse.ArgumentParser()
parser.add_argument("--history-days", type=int, default=14, help="how far back to start when no CSV exists yet")
args = parser.parse_args()

API_KEY = os.environ["ENTSOE_API_KEY"]
started = time.monotonic()

old = pd.DataFrame(columns=["zone", "time", "price_eur_mwh"])
if CSV.exists():
    old = pd.read_csv(CSV, parse_dates=["time"])
    old["time"] = pd.to_datetime(old["time"], utc=True)

today = pd.Timestamp.now(tz=MARKET_TZ).normalize()
floor = today - pd.Timedelta(days=KEEP_DAYS)
end = today + pd.Timedelta(days=2)  # exclusive; covers all of tomorrow

# per zone: newest stored price (NaT if none). Stalest zones go first so that, if the
# API is slow and the time budget runs out, the same zones don't starve on every run.
latest = old.groupby("zone")["time"].max().reindex(ZONE_CODES)
order = sorted(ZONE_CODES, key=lambda z: latest[z] if pd.notna(latest[z]) else pd.Timestamp(0, tz="UTC"))


def start_for(zone):
    if pd.isna(latest[zone]):
        return today - pd.Timedelta(days=args.history_days)
    return max(latest[zone].tz_convert(MARKET_TZ).normalize() - pd.Timedelta(days=OVERLAP_DAYS), floor)


def fetch_zone(zone):
    """-> (zone, status, hourly series). status: ok | no_data | error | skipped"""
    if time.monotonic() - started > TIME_BUDGET_S:
        return zone, "skipped", None
    client = EntsoePandasClient(api_key=API_KEY, timeout=60, retry_count=2, retry_delay=5)  # one per thread
    for attempt in range(3):
        try:
            series = client.query_day_ahead_prices(zone, start=start_for(zone), end=end)
        except NoMatchingDataError:
            return zone, "no_data", None  # nothing published for this window
        except Exception as exc:  # network / rate limit / 5xx: back off and retry
            print(f"  {zone}: {type(exc).__name__} (attempt {attempt + 1})", file=sys.stderr, flush=True)
            time.sleep(5 * (attempt + 1))
            continue
        hourly = series.resample("h").mean().dropna()
        return (zone, "ok", hourly) if len(hourly) else (zone, "no_data", None)
    return zone, "error", None


print(f"Fetching {len(order)} zones with {WORKERS} workers, up to {(end - pd.Timedelta(days=1)).date()}", flush=True)
frames, failed = [], {"no_data": [], "error": [], "skipped": []}
with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    for zone, status, hourly in pool.map(fetch_zone, order):
        if status != "ok":
            failed[status].append(zone)
            continue
        frames.append(pd.DataFrame({
            "zone": zone,
            "time": hourly.index.tz_convert("UTC"),
            "price_eur_mwh": hourly.to_numpy(),
        }))

if not frames:
    sys.exit("No data returned for any zone - check ENTSOE_API_KEY and ENTSO-E availability")

new = pd.concat(frames, ignore_index=True)
merged = pd.concat([old, new], ignore_index=True)
merged = merged.drop_duplicates(["zone", "time"], keep="last")  # fresh values win
merged = merged[merged["time"] >= (today - pd.Timedelta(days=KEEP_DAYS)).tz_convert("UTC")]
merged = merged.sort_values(["zone", "time"])
merged["price_eur_mwh"] = merged["price_eur_mwh"].round(2)

CSV.parent.mkdir(exist_ok=True)
merged.to_csv(CSV, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
print(f"{new['zone'].nunique()}/{len(ZONE_CODES)} zones fetched, {len(merged)} rows stored in {CSV.name}")
for status, zones in failed.items():
    if zones:
        print(f"{status}: {', '.join(zones)}")
if failed["error"] or failed["skipped"]:
    print("Some zones were not refreshed; the next scheduled run will pick them up first.")
