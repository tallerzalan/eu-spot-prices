"""Fetch ECB euro reference rates into data/fx.csv (columns: date, currency, per_eur).

per_eur = units of the currency for 1 EUR, so EUR = local amount / per_eur.
The ECB publishes on TARGET business days around 16:00 CET; the 90-day file is merged into what is stored,
so history accumulates. No API key needed. The app forward-fills weekends and holidays.
"""
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "fx.csv"
URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"

with urllib.request.urlopen(URL, timeout=60) as resp:
    tree = ET.parse(resp)

rows = []
for node in tree.iter():
    if node.tag.endswith("Cube") and "time" in node.attrib:
        for c in node:
            rows.append((node.attrib["time"], c.attrib["currency"], float(c.attrib["rate"])))
if not rows:
    sys.exit("No rates found in the ECB file")

new = pd.DataFrame(rows, columns=["date", "currency", "per_eur"])
if CSV.exists():
    new = pd.concat([pd.read_csv(CSV), new], ignore_index=True)
new = new.drop_duplicates(["date", "currency"], keep="last").sort_values(["date", "currency"])
new = new[new["date"] >= (pd.Timestamp.now() - pd.Timedelta(days=120)).strftime("%Y-%m-%d")]
CSV.parent.mkdir(exist_ok=True)
new.to_csv(CSV, index=False)
print(f"{new['date'].nunique()} days, {new['currency'].nunique()} currencies, latest {new['date'].max()}")
