"""Build data/zones.geojson from Natural Earth (public domain).

One-off tool, not needed at runtime. Usage:
    python -I scripts/build_geo.py <dir with ne_10m_admin_0_countries.geojson
                                    and ne_10m_admin_1_states_provinces.geojson>

Zones that are not whole countries (SE_1-4, NO_1-5, DK_1/2, Italian zones) are
approximated by grouping admin-1 regions (counties / regions), so their borders
follow administrative lines and not the exact grid boundaries.
"""
import json
import sys
import unicodedata
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from zones import ZONE_NAME, ZONE_REGION  # noqa: E402

src = Path(sys.argv[1])
out = ROOT / "data" / "zones.geojson"


def ascii_(s):
    s = str(s).replace(chr(0xF8), "o").replace(chr(0xD8), "O").replace(chr(0xE6), "ae").replace(chr(0xC6), "AE")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


# whole-country zones: zone -> Natural Earth ADMIN name(s)
COUNTRIES = {
    "AL": ["Albania"], "AT": ["Austria"], "BE": ["Belgium"], "BG": ["Bulgaria"],
    "CH": ["Switzerland"], "CZ": ["Czechia"], "DE_LU": ["Germany", "Luxembourg"],
    "EE": ["Estonia"], "ES": ["Spain"], "FI": ["Finland"], "FR": ["France"],
    "GR": ["Greece"], "HR": ["Croatia"], "HU": ["Hungary"], "IE_SEM": ["Ireland"],
    "LT": ["Lithuania"], "LV": ["Latvia"], "MD": ["Moldova"], "ME": ["Montenegro"],
    "MK": ["North Macedonia"], "NL": ["Netherlands"], "PL": ["Poland"],
    "PT": ["Portugal"], "RO": ["Romania"], "RS": ["Republic of Serbia"],
    "SI": ["Slovenia"], "SK": ["Slovakia"], "XK": ["Kosovo"],
}
NORTHERN_IRELAND = ["Derry", "Fermanagh", "Armagh", "Belfast", "North Down", "Down", "Antrim"]

# split countries: admin-1 names (ASCII-folded) per zone
SPLITS = {
    "Sweden": {
        "SE_1": ["Norrbotten", "Vasterbotten"],
        "SE_2": ["Jamtland", "Vasternorrland", "Gavleborg"],
        "SE_3": ["Dalarna", "Varmland", "Orebro", "Vastmanland", "Uppsala", "Stockholm",
                 "Sodermanland", "Ostergotland", "Vastra Gotaland", "Jonkoping", "Gotland"],
        "SE_4": ["Skane", "Blekinge", "Halland", "Kronoberg", "Kalmar"],
    },
    "Norway": {
        "NO_1": ["Oslo", "Akershus", "Ostfold", "Buskerud", "Vestfold", "Hedmark", "Oppland"],
        "NO_2": ["Telemark", "Aust-Agder", "Vest-Agder", "Rogaland"],
        "NO_3": ["More og Romsdal", "Nord-Trondelag", "Sor-Trondelag"],
        "NO_4": ["Nordland", "Troms", "Finnmark"],
        "NO_5": ["Hordaland", "Sogn og Fjordane"],
    },
    "Denmark": {
        "DK_1": ["Nordjylland", "Midtjylland", "Syddanmark"],
        "DK_2": ["Sjaaelland", "Hovedstaden"],
    },
}
ITALY_REGIONS = {
    "IT_NORD": ["Valle d'Aosta", "Piemonte", "Liguria", "Lombardia", "Trentino-Alto Adige",
                "Veneto", "Friuli-Venezia Giulia", "Emilia-Romagna"],
    "IT_CNOR": ["Toscana", "Umbria", "Marche"],
    "IT_CSUD": ["Lazio", "Abruzzo", "Campania"],
    "IT_SUD": ["Molise", "Apulia", "Basilicata"],
    "IT_CALA": ["Calabria"],
    "IT_SICI": ["Sicily"],
    "IT_SARD": ["Sardegna"],
}

a0 = gpd.read_file(src / "ne_10m_admin_0_countries.geojson")
a1 = gpd.read_file(src / "ne_10m_admin_1_states_provinces.geojson")
a1["name_a"] = a1["name"].map(ascii_)

geoms = {}
for zone, names in COUNTRIES.items():
    geoms[zone] = a0[a0["ADMIN"].isin(names)].geometry.union_all()
ni = a1[(a1["admin"] == "United Kingdom") & a1["name"].isin(NORTHERN_IRELAND)].geometry.union_all()
geoms["IE_SEM"] = geoms["IE_SEM"].union(ni)

for country, zmap in SPLITS.items():
    sub = a1[a1["admin"] == country]
    for zone, names in zmap.items():
        sel = sub[sub["name_a"].isin(names)]
        assert len(sel) == len(names), (zone, set(names) - set(sel["name_a"]))
        geoms[zone] = sel.geometry.union_all()

it = a1[a1["admin"] == "Italy"]
for zone, regions in ITALY_REGIONS.items():
    sel = it[it["region"].isin(regions)]
    assert sel["region"].nunique() == len(regions), zone
    geoms[zone] = sel.geometry.union_all()

missing = set(ZONE_NAME) - set(geoms)
assert not missing, missing

# keep mainland Europe only (drops overseas territories), then simplify
europe = box(-12, 34, 34, 72)
rows = []
for zone, geom in geoms.items():
    g = gpd.GeoSeries([geom.intersection(europe)]).simplify(0.02, preserve_topology=True).iloc[0]
    rows.append({"id": zone, "zone": zone, "name": ZONE_NAME[zone],
                 "region": ZONE_REGION[zone], "geometry": g})

gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
out.parent.mkdir(exist_ok=True)
out.unlink(missing_ok=True)
gdf.drop(columns="id").to_file(out, driver="GeoJSON", COORDINATE_PRECISION=3)

# make sure every feature carries a top-level id (needed by plotly's default featureidkey)
fc = json.loads(out.read_text(encoding="utf-8"))
for f in fc["features"]:
    f["id"] = f["properties"]["zone"]
out.write_text(json.dumps(fc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
print(f"{len(fc['features'])} zones -> {out} ({out.stat().st_size / 1024:.0f} KB)")
