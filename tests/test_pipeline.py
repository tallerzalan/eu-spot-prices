"""Offline tests for the fetch pipeline (no network, no API key)."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.base import Dataset, spread_to_quarters  # noqa: E402
from pipeline.regulation import parse_series  # noqa: E402


def test_spread_hourly_prices_repeat_into_quarters():
    s = pd.Series([10.0, 20.0], index=pd.date_range("2026-10-01", periods=2, freq="h", tz="UTC"))
    q = spread_to_quarters(s)
    assert len(q) == 8
    assert q.resample("h").mean().tolist() == [10.0, 20.0]


def test_spread_hourly_volumes_keep_their_sum():
    s = pd.Series([4.0, 8.0], index=pd.date_range("2026-10-01", periods=2, freq="h", tz="UTC"))
    q = spread_to_quarters(s, additive=True)
    assert q.resample("h").sum().tolist() == [4.0, 8.0]


def test_quarter_series_is_untouched():
    s = pd.Series(range(8), index=pd.date_range("2026-10-01", periods=8, freq="15min", tz="UTC"), dtype=float)
    assert spread_to_quarters(s).equals(s)


class FakeDataset(Dataset):
    name = "fake"
    columns = ["price", "volume"]
    hourly_agg = {"price": "mean", "volume": "sum"}
    quarter_file = "fake_15min.csv"
    hourly_file = "fake.csv"

    def fetch_zone(self, zone, start, end):
        if zone == "XX":
            return None
        index = pd.date_range(self.today.tz_convert("UTC") - pd.Timedelta(hours=2), periods=8, freq="15min")
        return pd.DataFrame({"price": [1.0, 2.0, 3.0, 4.0] * 2, "volume": [1.0] * 8}, index=index)


def test_dataset_run_writes_quarter_and_hourly_files(tmp_path):
    ds = FakeDataset("key", data_dir=tmp_path, zones=["FR", "XX"])
    assert ds.run() == 0
    assert ds.zone_status == {"FR": "ok", "XX": "no_data"}
    quarter = pd.read_csv(tmp_path / "fake_15min.csv")
    hourly = pd.read_csv(tmp_path / "fake.csv")
    assert len(quarter) == 8 and set(quarter["zone"]) == {"FR"}
    assert hourly["price"].tolist() == [2.5, 2.5] and hourly["volume"].tolist() == [4.0, 4.0]


def test_second_run_merges_without_duplicates(tmp_path):
    FakeDataset("key", data_dir=tmp_path, zones=["FR"]).run()
    FakeDataset("key", data_dir=tmp_path, zones=["FR"]).run()
    assert len(pd.read_csv(tmp_path / "fake_15min.csv")) == 8


BALANCING_XML = b"""<?xml version="1.0"?><Balancing_MarketDocument xmlns="urn:x"><TimeSeries>
<flowDirection.direction>A01</flowDirection.direction><Period><timeInterval><start>2026-10-27T00:00Z</start>
<end>2026-10-27T00:05Z</end></timeInterval><resolution>PT1M</resolution>
<Point><position>1</position><quantity>10</quantity></Point><Point><position>3</position><quantity>30</quantity></Point>
</Period></TimeSeries></Balancing_MarketDocument>"""


def test_parse_series_fills_sparse_blocks_forward():
    [(direction, _, series)] = parse_series(BALANCING_XML)
    assert direction == 1
    assert series.tolist() == [10.0, 10.0, 30.0, 30.0, 30.0]
