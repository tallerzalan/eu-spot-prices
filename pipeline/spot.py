"""Day-ahead (spot) prices: data/prices_15min.csv and data/prices.csv (hourly means)."""
import pandas as pd

from .base import MARKET_TZ, Dataset, retrying, spread_to_quarters


class SpotPrices(Dataset):
    name = "spot"
    columns = ["price_eur_mwh"]
    hourly_agg = {"price_eur_mwh": "mean"}
    quarter_file = "prices_15min.csv"
    hourly_file = "prices.csv"
    hourly_keep_days = 60
    hourly_history_days = 14
    hourly_target_days = 45         # the imbalance premium needs spot back to the start of the imbalance history
    window_end_days = 2             # tomorrow is published around 13:00 CET

    def fetch_zone(self, zone, start, end):
        series = retrying(lambda: self.pandas_client().query_day_ahead_prices(zone, start=start, end=end), zone)
        if series is None or not len(series):
            return None
        return spread_to_quarters(series.tz_convert("UTC")).to_frame("price_eur_mwh")

    def is_up_to_date(self):
        """Tomorrow's prices are in for every zone."""
        old = self._read(self.hourly_file)
        newest = old.groupby("zone")["time"].max().reindex(self.zones)
        tomorrow = (self.today + pd.Timedelta(days=1)).date()
        return bool(newest.notna().all() and (newest.dt.tz_convert(MARKET_TZ).dt.date >= tomorrow).all())
