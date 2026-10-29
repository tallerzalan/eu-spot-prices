"""ECB euro reference rates -> data/fx.csv (columns: date, currency, per_eur). No API key needed.

per_eur = units of the currency for 1 EUR, so EUR = local amount / per_eur. The ECB publishes on TARGET business
days around 16:00 CET; the 90-day file is merged into what is stored. The dashboard forward-fills weekends.
"""
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

from .base import ROOT

URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"


def run_fx(data_dir=None) -> int:
    csv = (Path(data_dir) if data_dir else ROOT / "data") / "fx.csv"
    with urllib.request.urlopen(URL, timeout=60) as resp:
        tree = ET.parse(resp)
    rows = [(node.attrib["time"], c.attrib["currency"], float(c.attrib["rate"]))
            for node in tree.iter() if node.tag.endswith("Cube") and "time" in node.attrib for c in node]
    if not rows:
        print("[fx] no rates found in the ECB file")
        return 1
    new = pd.DataFrame(rows, columns=["date", "currency", "per_eur"])
    if csv.exists():
        new = pd.concat([pd.read_csv(csv), new], ignore_index=True)
    new = new.drop_duplicates(["date", "currency"], keep="last").sort_values(["date", "currency"])
    new = new[new["date"] >= (pd.Timestamp.now() - pd.Timedelta(days=120)).strftime("%Y-%m-%d")]
    csv.parent.mkdir(exist_ok=True)
    new.to_csv(csv, index=False)
    print(f"[fx] {new['date'].nunique()} days, {new['currency'].nunique()} currencies, latest {new['date'].max()}")
    return 0
