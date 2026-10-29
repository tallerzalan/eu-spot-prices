"""Regulation data: data/regulation_15min.csv and regulation_status.json.

ENTSO-E's REST API does not publish activated balancing energy volumes (17.1.E): the document type is rejected for
every parameter combination (checked 2026-10-29, see CLAUDE.md). What it does publish, and what is stored here:

  state_up_mw / state_down_mw   "Current balancing state" [12.3.A]: the TSO's minute-by-minute balancing need.
                                 Up = the system is short and needs upregulation, down = it is long and needs
                                 downregulation. Mean MW per 15 minutes over the 1-minute values.
  afrr_bid_up_mw, afrr_bid_down_mw, mfrr_bid_up_mw, mfrr_bid_down_mw
                                 "Aggregated balancing energy bids" [12.3.E]: the volume of offers available for
                                 upward / downward regulation per product (MW, summed over the market products).
"""
import io
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd
from entsoe.mappings import lookup_area

from .base import Dataset, retrying, spread_to_quarters

BID_PRODUCTS = {"A51": "afrr", "A47": "mfrr"}  # process type -> column prefix
DIRECTION = {"A01": 1, "A02": -1}              # up / down
# dashboard zone -> ENTSO-E areas whose values are added up (default: the zone itself)
AREAS = {"DE_LU": ["DE_50HZ", "DE_AMPRION", "DE_TENNET", "DE_TRANSNET"]}


def parse_series(content: bytes):
    """ENTSO-E balancing document (XML or ZIP of XMLs) -> list of (direction, product key, Series in UTC).

    Series are expanded to every step of their period: blocks of curve type A03 only list the points where the
    value changes, the value then holds until the next point.
    """
    blobs = [content]
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            blobs = [zf.read(n) for n in zf.namelist() if n.endswith("xml")]
    out = []
    for blob in blobs:
        root = ET.fromstring(blob)
        for ts in root.iter():
            if ts.tag.split("}")[-1] != "TimeSeries":
                continue
            tags = {c.tag.split("}")[-1]: (c.text or "").strip() for c in ts}
            product = tags.get("standard_MarketProduct.marketProductType") or tags.get("original_MarketProduct.marketProductType") or ""
            for period in (p for p in ts if p.tag.split("}")[-1] == "Period"):
                interval = next(c for c in period if c.tag.split("}")[-1] == "timeInterval")
                bounds = {c.tag.split("}")[-1]: c.text for c in interval}
                start, stop = pd.Timestamp(bounds["start"]), pd.Timestamp(bounds["end"])
                res = {c.tag.split("}")[-1]: c.text for c in period}["resolution"]
                step = pd.Timedelta(res.replace("PT", "").lower().replace("m", "min").replace("h", "h"))
                values = {}
                for point in (p for p in period if p.tag.split("}")[-1] == "Point"):
                    f = {c.tag.split("}")[-1]: c.text for c in point}
                    values[start + (int(f["position"]) - 1) * step] = float(f["quantity"])
                if not values:
                    continue
                full = pd.date_range(start, stop, freq=step, inclusive="left")
                series = pd.Series(values).reindex(full).ffill()
                out.append((DIRECTION.get(tags.get("flowDirection.direction")), product, series.dropna()))
    return out


def day_chunks(start, end, days):
    cursor = start
    while cursor < end:
        yield cursor, min(cursor + pd.Timedelta(days=days), end)
        cursor += pd.Timedelta(days=days)


class Regulation(Dataset):
    name = "regulation"
    columns = ["state_up_mw", "state_down_mw", "afrr_bid_up_mw", "afrr_bid_down_mw", "mfrr_bid_up_mw",
               "mfrr_bid_down_mw"]
    quarter_file = "regulation_15min.csv"
    hourly_file = None              # the dashboard averages the quarters to hours
    decimals = 1
    quarter_history_days = 14       # the first fetch is large (1-minute data); the file keeps 30 days
    overlap_days = 1
    time_budget_s = 20 * 60

    def request(self, client, params, c_start, c_end):
        return retrying(lambda: client._base_request(params=dict(params), start=c_start, end=c_end).content,
                        f"{params.get('area_Domain')} {params.get('documentType')}")

    def balancing_state(self, client, code, start, end):
        """Minute values signed (up +, down -) -> quarter-hourly up and down means (MW)."""
        minutes = []
        for c_start, c_end in day_chunks(start, end, 7):
            content = self.request(client, {"documentType": "A86", "businessType": "B33", "area_Domain": code,
                                            "curveType": "A03"}, c_start, c_end)
            for direction, _, series in (parse_series(content) if content else []):
                if direction:
                    minutes.append(series * direction)
        if not minutes:
            return None
        signed = pd.concat(minutes).groupby(level=0).sum().sort_index()
        signed.index = signed.index.tz_convert("UTC") if signed.index.tz else signed.index.tz_localize("UTC")
        return pd.DataFrame({"state_up_mw": signed.clip(lower=0).resample("15min").mean(),
                             "state_down_mw": (-signed).clip(lower=0).resample("15min").mean()}).dropna(how="all")

    def bids(self, client, code, start, end):
        """Offered volume per direction (MW), summed over the products of one reserve type."""
        cols = {}
        for process, key in BID_PRODUCTS.items():
            per_direction = {1: [], -1: []}
            for c_start, c_end in day_chunks(start, end, 7):
                content = self.request(client, {"documentType": "A24", "processType": process, "area_Domain": code,
                                                "curveType": "A03"}, c_start, c_end)
                for direction, _, series in (parse_series(content) if content else []):
                    if direction:
                        per_direction[direction].append(series)
            for direction, name in ((1, "up"), (-1, "down")):
                if per_direction[direction]:
                    total = pd.concat(per_direction[direction]).groupby(level=0).sum().sort_index()
                    total.index = total.index.tz_convert("UTC")
                    cols[f"{key}_bid_{name}_mw"] = spread_to_quarters(total)
        return pd.DataFrame(cols) if cols else None

    def fetch_zone(self, zone, start, end):
        client = self.raw_client()
        parts = []
        for area in AREAS.get(zone, [zone]):
            code = lookup_area(area).code
            frames = [f for f in (self.balancing_state(client, code, start, end),
                                  self.bids(client, code, start, end)) if f is not None]
            if frames:
                parts.append(pd.concat(frames, axis=1))
        if not parts:
            return None
        combined = pd.concat(parts).groupby(level=0).sum(min_count=1).sort_index()
        self.zone_info[zone] = {"series": [c for c in combined.columns if combined[c].notna().any()]}
        return combined

    def finish(self):
        checked = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%MZ")
        status = {z: {"status": s, "checked": checked, **self.zone_info.get(z, {})}
                  for z, s in self.zone_status.items() if s != "skipped"}
        self.write_json("regulation_status.json", dict(sorted(status.items())))
