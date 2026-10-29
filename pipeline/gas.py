"""Gas prices (TTF and NBP front-month futures): data/gas.csv. No API key needed.

Source: Yahoo Finance chart API (free, unofficial, delayed). TTF (ICE Endex, EUR/MWh) has hourly bars; NBP (ICE UK,
pence/therm) only has daily settlement bars there, so hourly runs just refresh the latest day. Prices are stored as
quoted; the dashboard converts NBP to EUR/MWh with the ECB GBP rate.

Columns: market, time (UTC bar start), open, high, low, close.
"""
import json
import time
import urllib.request
from pathlib import Path

import pandas as pd

from .base import ROOT

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}"
SOURCES = {
    "TTF": dict(symbol="TTF=F", interval="1h", range="3mo", keep_days=90),
    "NBP": dict(symbol="UKG=F", interval="1d", range="1y", keep_days=365),
}
COLUMNS = ["open", "high", "low", "close"]


def fetch_bars(symbol, interval, range_):
    request = urllib.request.Request(URL.format(symbol=symbol, range=range_, interval=interval),
                                     headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                result = json.load(resp)["chart"]["result"]
            break
        except Exception as exc:
            print(f"  {symbol}: {type(exc).__name__} (attempt {attempt + 1})", flush=True)
            time.sleep(5 * (attempt + 1))
    else:
        raise RuntimeError(f"{symbol}: retries exhausted")
    if not result or not result[0].get("timestamp"):
        return pd.DataFrame(columns=COLUMNS)
    quote = result[0]["indicators"]["quote"][0]
    df = pd.DataFrame({c: quote.get(c) for c in COLUMNS},
                      index=pd.to_datetime(result[0]["timestamp"], unit="s", utc=True))
    df = df.dropna(subset=["close"])
    if interval == "1h":  # Yahoo appends the live quote as an off-the-hour bar; the hour's own bar has the same value
        df = df[(df.index.minute == 0) & (df.index.second == 0)]
    return df


def run_gas(data_dir=None) -> int:
    csv = (Path(data_dir) if data_dir else ROOT / "data") / "gas.csv"
    old = pd.read_csv(csv, parse_dates=["time"]) if csv.exists() else pd.DataFrame(columns=["market", "time"] + COLUMNS)
    old["time"] = pd.to_datetime(old["time"], utc=True)
    frames, errors = [], []
    for market, cfg in SOURCES.items():
        try:
            bars = fetch_bars(cfg["symbol"], cfg["interval"], cfg["range"])
        except Exception as exc:
            errors.append(market)
            print(f"[gas] {market}: {exc}")
            continue
        print(f"[gas] {market}: {len(bars)} bars, latest {bars.index.max() if len(bars) else '-'}")
        frames.append(bars.rename_axis("time").reset_index().assign(market=market))
    if frames:
        merged = pd.concat([old] + frames, ignore_index=True).drop_duplicates(["market", "time"], keep="last")
        keep = {m: pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=c["keep_days"]) for m, c in SOURCES.items()}
        merged = merged[merged["time"] >= merged["market"].map(keep)].sort_values(["market", "time"])
        merged[COLUMNS] = merged[COLUMNS].astype(float).round(3)
        csv.parent.mkdir(exist_ok=True)
        merged[["market", "time"] + COLUMNS].to_csv(csv, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
    return 1 if errors and not frames else 0
