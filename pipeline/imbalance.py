"""Imbalance prices and volumes: data/imbalance_15min.csv, data/imbalance.csv and imbalance_currency.json.

price_long / price_short: imbalance price for parties that are long (surplus) / short (deficit); single-price
systems publish one price, stored in both columns. volume_mwh: net system imbalance, positive = surplus.
ENTSO-E quotes the prices in each country's own currency; the currency per zone is kept next to the data and
converted to EUR by the dashboard.
"""
import io
import json
import re
import zipfile

import pandas as pd

from .base import Dataset, retrying, spread_to_quarters


class Imbalance(Dataset):
    name = "imbalance"
    columns = ["price_long", "price_short", "volume_mwh"]
    hourly_agg = {"price_long": "mean", "price_short": "mean", "volume_mwh": "sum"}
    quarter_file = "imbalance_15min.csv"
    hourly_file = "imbalance.csv"
    hourly_keep_days = 45
    hourly_history_days = 35
    overlap_days = 2

    def prepare(self):
        path = self.data_dir / "imbalance_currency.json"
        self.currency = json.loads(path.read_text()) if path.exists() else {}

    def finish(self):
        self.write_json("imbalance_currency.json", dict(sorted(self.currency.items())))

    def detect_currency(self, zone):
        """The currency tag sits in the raw XML, not in the pandas result."""
        blob = retrying(lambda: self.raw_client().query_imbalance_prices(
            zone, start=self.today - pd.Timedelta(days=3), end=self.today), f"{zone} currency")
        if not blob:
            return None
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for name in zf.namelist():
                m = re.search(rb"<currency_Unit\.name>\s*([A-Za-z]{3})\s*<", zf.read(name), re.IGNORECASE)
                if m:
                    return m.group(1).decode().upper()
        return None

    def fetch_zone(self, zone, start, end):
        client = self.pandas_client()
        prices = retrying(lambda: client.query_imbalance_prices(zone, start=start, end=end), f"{zone} prices")
        volumes = retrying(lambda: client.query_imbalance_volumes(zone, start=start, end=end), f"{zone} volumes")
        parts = {}
        if prices is not None and len(prices):
            prices = prices.tz_convert("UTC")
            long_ = prices["Long"] if "Long" in prices else None
            short = prices["Short"] if "Short" in prices else None
            if long_ is None and short is None:  # single-price system: one column, used for both
                long_ = short = prices.iloc[:, 0]
            parts["price_long"] = spread_to_quarters(long_ if long_ is not None else short)
            parts["price_short"] = spread_to_quarters(short if short is not None else long_)
            if zone not in self.currency:
                found = self.detect_currency(zone)
                if found:
                    self.currency[zone] = found
        if volumes is not None and len(volumes):
            vol = volumes["Imbalance Volume"] if isinstance(volumes, pd.DataFrame) else volumes
            parts["volume_mwh"] = spread_to_quarters(vol.tz_convert("UTC"), additive=True)
        return pd.DataFrame(parts).dropna(how="all") if parts else None
