"""Helpers for quarter-hourly (15-minute) data shared by the fetch scripts."""
import pandas as pd

QUARTER = pd.Timedelta(minutes=15)


def to_quarter(s: pd.Series, additive: bool = False) -> pd.Series:
    """Bring a series to 15-minute steps.

    Series that are already 15-minute stay as they are. Coarser series (30/60 minute zones, or the hourly part
    before the 15-minute market started) are repeated into the quarters they cover. `additive` series (volumes
    in MWh) are split evenly over those quarters so that sums over an hour stay unchanged.
    """
    s = s.dropna().sort_index()
    if len(s) < 2:
        return s
    step = s.index.to_series().diff().median()
    k = int(round(step / QUARTER))
    if k <= 1:
        return s
    parts = [(s / k if additive else s).set_axis(s.index + i * QUARTER) for i in range(k)]
    return pd.concat(parts).sort_index()
