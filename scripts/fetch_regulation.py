"""Fetch activated balancing energy (regulation volumes) into data/regulation_15min.csv.

ENTSO-E "Activated Balancing Energy" [17.1.E]: the energy the TSO actually activated per reserve product,
split by direction. Columns of the CSV (MWh per 15 minutes, always positive):
  zone, time (UTC), afrr_up, afrr_down, mfrr_up, mfrr_down, rr_up, rr_down
  - aFRR = automatic frequency restoration reserve (business type A96)
  - mFRR = manual frequency restoration reserve   (A97)
  - RR   = replacement reserve                    (A98)
  - up = upregulation (more power into the grid), down = downregulation
Zones that publish hourly or 30-minute volumes are spread evenly over the quarters, so sums stay correct.
Not every zone/product is published; data/regulation_status.json records what each zone returned.

Incremental like the other fetch scripts.  Usage:  ENTSOE_API_KEY=... python scripts/fetch_regulation.py
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from entsoe import EntsoeRawClient
from entsoe.mappings import BSNTYPE, lookup_area
from entsoe.parsers import parse_contracted_reserve, parse_contracted_reserve_zip

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from qh import to_quarter  # noqa: E402
from zones import ZONE_CODES  # noqa: E402

CSV = ROOT / "data" / "regulation_15min.csv"
STATUS = ROOT / "data" / "regulation_status.json"
MARKET_TZ = "Europe/Brussels"
KEEP_DAYS = 30
OVERLAP_DAYS = 2
TIME_BUDGET_S = 12 * 60
WORKERS = 6

PRODUCTS = {"A96": "afrr", "A97": "mfrr", "A98": "rr"}
DIRECTIONS = {"Up": "up", "Down": "down"}
# dashboard zone -> ENTSO-E areas whose volumes are added up (default: the zone itself)
AREAS = {"DE_LU": ["DE_50HZ", "DE_AMPRION", "DE_TENNET", "DE_TRANSNET"]}
# queries that mean "nothing published / not supported" rather than a temporary failure
NO_DATA_ERRORS = {"NoMatchingDataError", "InvalidBusinessParameterError", "InvalidPSRTypeError", "PaginationError"}
COLS = [f"{p}_{d}" for p in PRODUCTS.values() for d in DIRECTIONS.values()]

API_KEY = os.environ["ENTSOE_API_KEY"]
started = time.monotonic()

old = pd.DataFrame(columns=["zone", "time"] + COLS)
if CSV.exists():
    old = pd.read_csv(CSV, parse_dates=["time"])
    old["time"] = pd.to_datetime(old["time"], utc=True)
status = json.loads(STATUS.read_text()) if STATUS.exists() else {}

today = pd.Timestamp.now(tz=MARKET_TZ).normalize()
floor = today - pd.Timedelta(days=KEEP_DAYS)
end = today + pd.Timedelta(days=1)
latest = old.groupby("zone")["time"].max().reindex(ZONE_CODES)
NEVER = pd.Timestamp(0, tz="UTC")
order = sorted(ZONE_CODES, key=lambda z: latest[z] if pd.notna(latest[z]) else NEVER)


def start_for(zone):
    if pd.isna(latest[zone]):
        return floor
    return max(latest[zone].tz_convert(MARKET_TZ).normalize() - pd.Timedelta(days=OVERLAP_DAYS), floor)


def parse(content, tz):
    """ENTSO-E answers with a ZIP of XML documents when there is more than one, else a single XML."""
    if content[:2] == b"PK":
        return parse_contracted_reserve_zip(content, tz, "quantity")
    return parse_contracted_reserve(content, tz, "quantity")


# ENTSO-E "Activated Balancing Energy" [17.1.E] needs a processType. entsoe-py leaves it out (HTTP 400), so
# the request is built here. Variants are tried in order; the first one ENTSO-E accepts is used.
PROCESS_TYPES = {"A96": "A51", "A97": "A47", "A98": "A46"}  # aFRR / mFRR / RR process types
CHUNK_DAYS = 7  # keep every response well below ENTSO-E's document limit
variant_used = {}


def request_variants(area_code, business_type):
    base = {"documentType": "A83", "controlArea_Domain": area_code}
    return [
        {**base, "processType": "A16", "businessType": business_type},
        {**base, "processType": "A16"},
        {**base, "processType": PROCESS_TYPES[business_type]},
    ]


def fetch_chunk(client, area, business_type, c_start, c_end):
    """-> DataFrame or None (nothing published). Raises if every request variant is rejected."""
    area_obj = lookup_area(area)
    errors = []
    for i, params in enumerate(request_variants(area_obj.code, business_type)):
        for attempt in range(3):
            try:
                content = client._base_request(params=dict(params), start=c_start, end=c_end).content
                df = parse(content, area_obj.tz).tz_convert(area_obj.tz)
                variant_used[business_type] = i
                if "businessType" not in params:  # response holds all products: keep the one asked for
                    df = df.loc[:, [c for c in df.columns if c[0] == BSNTYPE[business_type]]]
                return df if len(df.columns) and len(df) else None
            except Exception as exc:
                name = type(exc).__name__
                if name == "NoMatchingDataError":
                    return None
                detail = getattr(getattr(exc, "response", None), "text", "") or ""
                msg = f"{name}: {exc}"[:90] + (f" | {' '.join(detail.split())[:240]}" if detail else "")
                if name in NO_DATA_ERRORS or "400" in str(exc):
                    errors.append(f"variant {i}: {msg}")
                    break  # this variant is rejected: try the next one
                time.sleep(5 * (attempt + 1))
                errors.append(f"variant {i}: {msg}")
    raise RuntimeError(f"{area}/{business_type}: " + " || ".join(errors[-3:]))


def query(client, area, business_type, start):
    """One product for one area over [start, end) -> DataFrame (columns: product name, direction) or None."""
    frames, chunk_start = [], start
    while chunk_start < end:
        chunk_end = min(chunk_start + pd.Timedelta(days=CHUNK_DAYS), end)
        df = fetch_chunk(client, area, business_type, chunk_start, chunk_end)
        if df is not None:
            frames.append(df)
        chunk_start = chunk_end
    if not frames:
        return None
    df = pd.concat(frames)
    return df.truncate(before=start, after=end)


def fetch_zone(zone):
    """-> (zone, status, quarter-hourly DataFrame, info). status: ok | no_data | error | skipped"""
    if time.monotonic() - started > TIME_BUDGET_S:
        return zone, "skipped", None, None
    client = EntsoeRawClient(api_key=API_KEY, timeout=60, retry_count=2, retry_delay=5)
    start = start_for(zone)
    try:
        cols = {}
        seen = []
        for code, key in PRODUCTS.items():
            parts = [query(client, a, code, start) for a in AREAS.get(zone, [zone])]
            parts = [p for p in parts if p is not None and len(p)]
            if not parts:
                continue
            df = pd.concat(parts)
            for col in df.columns:
                seen.append("/".join(map(str, col)))
                direction = DIRECTIONS.get(col[1])
                if direction is None:
                    continue  # symmetric products are not shown
                s = df[col].groupby(level=0).sum(min_count=1).dropna()
                s.index = s.index.tz_convert("UTC")
                cols[f"{key}_{direction}"] = to_quarter(s, additive=True)
        if not cols:
            return zone, "no_data", None, {"columns": seen}
        out = pd.DataFrame(cols).reindex(columns=COLS)
        return zone, "ok", out, {"columns": seen}
    except Exception as exc:
        print(f"  {zone}: giving up ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
        return zone, "error", None, {"error": f"{type(exc).__name__}: {exc}"[:200]}


print(f"Fetching regulation volumes for {len(order)} zones with {WORKERS} workers", flush=True)
frames, failed = [], {"no_data": [], "error": [], "skipped": []}
stamp = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%MZ")
with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    for zone, st, out, info in pool.map(fetch_zone, order):
        if st == "skipped":
            failed[st].append(zone)
            continue
        status[zone] = {"status": st, "checked": stamp, "variant": dict(variant_used), **(info or {})}
        if st != "ok":
            failed[st].append(zone)
            continue
        status[zone]["rows"] = int(out.notna().any(axis=1).sum())
        frames.append(out.rename_axis("time").reset_index().assign(zone=zone))

STATUS.parent.mkdir(exist_ok=True)
STATUS.write_text(json.dumps(dict(sorted(status.items())), indent=1))
if frames:
    new = pd.concat([f.reindex(columns=["zone", "time"] + COLS) for f in frames], ignore_index=True)
    merged = pd.concat([old, new], ignore_index=True).drop_duplicates(["zone", "time"], keep="last")
    merged = merged[merged["time"] >= floor.tz_convert("UTC")].sort_values(["zone", "time"])
    merged[COLS] = merged[COLS].round(3)
    merged.to_csv(CSV, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
    print(f"{new['zone'].nunique()} zones with regulation data, {len(merged)} rows stored in {CSV.name}")
else:
    print("No regulation data returned for any zone.")
for st, zs in failed.items():
    if zs:
        print(f"{st}: {', '.join(zs)}")
sys.exit(1 if failed["error"] and not frames else 0)
