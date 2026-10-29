"""Intraday auction prices (IDA1, IDA2, IDA3): data/ida_15min.csv.

ENTSO-E publishes the three SIDC intraday auctions only for a few zones (checked 2026-10-29 over all dashboard
zones: Spain only, as contract type A07 with classification sequence 1, 2, 3). DE_LU, AT and DK_2 expose a
"sequence 2" day-ahead document too, but its prices equal the day-ahead prices, so it is not an intraday auction.
Add a zone to `default_zones` when ENTSO-E starts publishing it.

IDA1 (15:00 D-1) and IDA2 (22:00 D-1) cover the whole delivery day D, IDA3 (10:00 D) the second half of the day.
"""
import pandas as pd

from .base import Dataset, retrying, spread_to_quarters


class IntradayAuctions(Dataset):
    name = "ida"
    columns = ["ida1", "ida2", "ida3"]
    quarter_file = "ida_15min.csv"
    hourly_file = None              # the dashboard averages the quarters to hours
    default_zones = ["ES"]
    quarter_history_days = 30
    overlap_days = 2
    window_end_days = 2             # IDA1 for tomorrow is published the afternoon before

    def fetch_zone(self, zone, start, end):
        client = self.pandas_client()
        parts = {}
        for sequence in (1, 2, 3):
            series = retrying(lambda: client.query_intraday_prices(zone, start=start, end=end, sequence=sequence),
                              f"{zone} IDA{sequence}")
            if series is not None and len(series):
                parts[f"ida{sequence}"] = spread_to_quarters(series.tz_convert("UTC"))
        return pd.DataFrame(parts) if parts else None
