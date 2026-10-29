"""Shared machinery of the ENTSO-E fetchers.

A `Dataset` knows how to fetch ONE zone at 15-minute resolution (`fetch_zone`). Everything around that is the
same for every dataset and lives here: choosing the start per zone (incremental, with a one-time backfill),
fetching all zones in parallel within a time budget (stalest zones first), deriving the hourly file, merging with
what is stored, applying the retention window and writing the CSV files.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from zones import ZONE_CODES

ROOT = Path(__file__).resolve().parent.parent
MARKET_TZ = "Europe/Brussels"
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NEVER = pd.Timestamp(0, tz="UTC")
# entsoe-py exception names that mean "ENTSO-E has nothing for this request", not "try again"
NO_DATA_ERRORS = {"NoMatchingDataError", "InvalidBusinessParameterError", "InvalidPSRTypeError", "PaginationError"}


def retrying(fn, label, attempts=3, wait=5):
    """Run an ENTSO-E query. Returns None when nothing is published; retries network errors and 5xx."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            body = getattr(getattr(exc, "response", None), "text", "") or ""
            if type(exc).__name__ in NO_DATA_ERRORS or "is not valid" in body or "No matching data" in body:
                return None
            print(f"  {label}: {type(exc).__name__} (attempt {attempt + 1})", file=sys.stderr, flush=True)
            time.sleep(wait * (attempt + 1))
    raise RuntimeError(f"{label}: retries exhausted")


def spread_to_quarters(s: pd.Series, additive: bool = False) -> pd.Series:
    """Bring a series to 15-minute steps.

    Series that are already 15-minute stay as they are. Coarser ones (30/60 minute zones, or the hourly part
    before the 15-minute market started) are repeated into the quarters they cover. `additive` series
    (volumes in MWh) are split evenly so that sums over an hour stay unchanged.
    """
    s = s.dropna().sort_index()
    if len(s) < 2:
        return s
    k = int(round(s.index.to_series().diff().median() / pd.Timedelta(minutes=15)))
    if k <= 1:
        return s
    parts = [(s / k if additive else s).set_axis(s.index + i * pd.Timedelta(minutes=15)) for i in range(k)]
    return pd.concat(parts).sort_index()


class Dataset:
    """One ENTSO-E data set stored per zone as quarter-hourly (and optionally hourly) CSV."""

    name = ""                       # command line name: python -m pipeline <name>
    columns: list = []              # value columns of the CSV files
    hourly_agg: dict = {}           # column -> "mean" | "sum" when deriving the hourly file
    quarter_file = ""               # data/<file>: zone,time(UTC),<columns> at 15 minutes
    hourly_file = None              # data/<file>: same columns at 1 hour (None = not stored)
    decimals = 2
    hourly_keep_days = 45
    hourly_history_days = 14        # first fetch of a zone
    hourly_target_days = None       # backfill zones whose stored history is shorter than this
    quarter_keep_days = 30
    quarter_history_days = 30       # first fetch / backfill depth of the 15-minute file
    overlap_days = 3                # re-fetch this many days before the newest stored value
    window_end_days = 1             # fetch up to this many days after today (day-ahead needs 2)
    workers = 6                     # ENTSO-E allows 400 requests per minute per token
    time_budget_s = 12 * 60         # stop starting new zones after this; what arrived is saved

    def __init__(self, api_key, data_dir=None, zones=None):
        self.api_key = api_key
        self.data_dir = Path(data_dir) if data_dir else ROOT / "data"
        self.zones = list(zones or ZONE_CODES)
        self.today = pd.Timestamp.now(tz=MARKET_TZ).normalize()
        self.end = self.today + pd.Timedelta(days=self.window_end_days)
        self.zone_status = {}       # zone -> ok | no_data | error | skipped
        self.zone_info = {}         # zone -> free-form details a subclass wants to keep

    # ---- to implement
    def fetch_zone(self, zone, start, end):
        """-> DataFrame at 15 minutes (UTC index, `columns`) or None when nothing is published."""
        raise NotImplementedError

    def prepare(self):
        """Hook before the run (load side files)."""

    def finish(self):
        """Hook after the run (write side files)."""

    # ---- helpers
    def pandas_client(self):
        from entsoe import EntsoePandasClient
        return EntsoePandasClient(api_key=self.api_key, timeout=60, retry_count=2, retry_delay=5)

    def raw_client(self):
        from entsoe import EntsoeRawClient
        return EntsoeRawClient(api_key=self.api_key, timeout=60, retry_count=2, retry_delay=5)

    def _read(self, filename):
        cols = ["zone", "time"] + self.columns
        path = self.data_dir / filename if filename else None
        if path is None or not path.exists():
            return pd.DataFrame(columns=cols)
        df = pd.read_csv(path, parse_dates=["time"])
        df["time"] = pd.to_datetime(df["time"], utc=True)
        return df

    def _start(self, old, zone, keep_days, history_days, target_days):
        """Start of the fetch window for one file: incremental, or a one-time (back)fill."""
        if old is None:
            return None
        g = old[old["zone"] == zone]["time"]
        floor = self.today - pd.Timedelta(days=keep_days)
        if g.empty:
            return self.today - pd.Timedelta(days=history_days)
        if target_days:
            target = self.today - pd.Timedelta(days=target_days)
            if g.min() > (target + pd.Timedelta(days=2)).tz_convert("UTC"):
                return target
        return max(g.max().tz_convert(MARKET_TZ).normalize() - pd.Timedelta(days=self.overlap_days), floor)

    def _starts(self, old_h, old_q):
        out = {}
        for zone in self.zones:
            candidates = [
                self._start(old_q, zone, self.quarter_keep_days, self.quarter_history_days, self.quarter_history_days),
                self._start(old_h, zone, self.hourly_keep_days, self.hourly_history_days, self.hourly_target_days)
                if self.hourly_file else None,
            ]
            out[zone] = min(c for c in candidates if c is not None)
        return out

    def _hourly(self, q):
        agg = {c: ("mean" if how == "mean" else (lambda x: x.sum(min_count=1))) for c, how in self.hourly_agg.items()}
        return q.resample("h").agg(agg).dropna(how="all")

    def _merge(self, old, new_frames, keep_days):
        new = pd.concat(new_frames, ignore_index=True) if new_frames else old.iloc[0:0]
        merged = pd.concat([old, new], ignore_index=True).drop_duplicates(["zone", "time"], keep="last")
        merged = merged[merged["time"] >= (self.today - pd.Timedelta(days=keep_days)).tz_convert("UTC")]
        merged = merged.sort_values(["zone", "time"])
        merged[self.columns] = merged[self.columns].astype(float).round(self.decimals)
        return merged

    def _write(self, df, filename):
        self.data_dir.mkdir(exist_ok=True)
        df.to_csv(self.data_dir / filename, index=False, date_format=TS_FORMAT)

    # ---- the run
    def run(self) -> int:
        started = time.monotonic()
        old_q = self._read(self.quarter_file)
        old_h = self._read(self.hourly_file) if self.hourly_file else None
        self.prepare()
        starts = self._starts(old_h, old_q)

        def stamp(frame, zone):
            return pd.DataFrame(frame).rename_axis("time").reset_index().assign(zone=zone)

        # stalest zones first, so a slow API does not starve the same zones on every run
        def staleness(zone):
            times = []
            for old in (old_q, old_h):
                if old is not None:
                    newest = old.loc[old["zone"] == zone, "time"].max()
                    times.append(newest if pd.notna(newest) else NEVER)
            return min(times)

        order = sorted(self.zones, key=staleness)

        def job(zone):
            if time.monotonic() - started > self.time_budget_s:
                return zone, "skipped", None
            try:
                q = self.fetch_zone(zone, starts[zone], self.end)
            except Exception as exc:
                print(f"  {zone}: giving up ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
                self.zone_info.setdefault(zone, {})["error"] = f"{type(exc).__name__}: {exc}"[:200]
                return zone, "error", None
            if q is None or q.empty:
                return zone, "no_data", None
            return zone, "ok", q.reindex(columns=self.columns)

        print(f"[{self.name}] fetching {len(order)} zones with {self.workers} workers", flush=True)
        frames_q, frames_h = [], []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for zone, status, q in pool.map(job, order):
                self.zone_status[zone] = status
                if status != "ok":
                    continue
                frames_q.append(stamp(q, zone))
                if self.hourly_file:
                    frames_h.append(stamp(self._hourly(q), zone))

        if frames_q:
            self._write(self._merge(old_q, frames_q, self.quarter_keep_days), self.quarter_file)
            if self.hourly_file:
                self._write(self._merge(old_h, frames_h, self.hourly_keep_days), self.hourly_file)
        self.finish()

        by_status = {s: [z for z in self.zones if self.zone_status.get(z) == s]
                     for s in ("ok", "no_data", "error", "skipped")}
        print(f"[{self.name}] {len(by_status['ok'])} zones updated"
              + "".join(f", {s}: {', '.join(zs)}" for s, zs in by_status.items() if zs and s != "ok"), flush=True)
        if by_status["error"] or by_status["skipped"]:
            print("Some zones were not refreshed; the next run picks them up first.")
        return 1 if by_status["error"] and not frames_q else 0

    def write_json(self, filename, obj):
        self.data_dir.mkdir(exist_ok=True)
        (self.data_dir / filename).write_text(json.dumps(obj, indent=1))
