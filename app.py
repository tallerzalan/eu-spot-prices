"""European day-ahead spot prices - Streamlit dashboard (data from ENTSO-E)."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from zones import REGION_ORDER, ZONE_CODES, ZONE_NAME, ZONE_REGION

ROOT = Path(__file__).parent
MARKET_TZ = "Europe/Brussels"
COLORSCALE = "RdYlGn_r"  # green = cheap, red = expensive

st.set_page_config(page_title="European spot prices", page_icon=":zap:", layout="wide")


def label(code: str) -> str:
    return f"{code} - {ZONE_NAME[code]}"


@st.cache_data(show_spinner=False)
def load_prices(mtime: float) -> pd.DataFrame:  # mtime busts the cache when the CSV changes
    df = pd.read_csv(ROOT / "data" / "prices.csv", parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(MARKET_TZ)
    df["date"] = df["time"].dt.date
    df["slot"] = df["time"].dt.strftime("%H:00")
    # on the autumn clock change the 02:00 hour occurs twice; keep the slots distinct
    df.loc[df.duplicated(["zone", "date", "slot"]), "slot"] += " (2)"
    return df.sort_values(["zone", "time"])


@st.cache_data(show_spinner=False)
def load_geo() -> dict:
    return json.loads((ROOT / "data" / "zones.geojson").read_text(encoding="utf-8"))


csv_path = ROOT / "data" / "prices.csv"
if not csv_path.exists():
    st.error("data/prices.csv not found - run scripts/fetch_prices.py first.")
    st.stop()
prices = load_prices(csv_path.stat().st_mtime)
geo = load_geo()

# ---------------------------------------------------------------- sidebar
dates = sorted(prices["date"].unique(), reverse=True)
coverage = prices.groupby("date")["zone"].nunique()
default_date = next((d for d in dates if coverage[d] >= 0.5 * len(ZONE_CODES)), dates[0])

with st.sidebar:
    st.header("Controls")
    day = st.selectbox("Delivery day", dates, index=dates.index(default_date),
                       format_func=lambda d: d.strftime("%a %d %b %Y"))
    day_df = prices[prices["date"] == day]
    slots = list(dict.fromkeys(day_df.sort_values("time")["slot"]))
    metric = st.radio("Map shows", ["Daily average", "Single hour"], horizontal=True)
    hour = None
    if metric == "Single hour":
        hour = st.select_slider("Hour (market time, CET/CEST)", options=slots, value=slots[min(12, len(slots) - 1)])
    st.caption(f"Zones with data this day: {day_df['zone'].nunique()} / {len(ZONE_CODES)}")
    st.caption(f"Latest price in dataset: {prices['time'].max():%a %d %b %H:%M} (market time)")

st.title("European day-ahead spot prices")
st.caption("EUR/MWh · hourly means · times in market time (CET/CEST) · source: ENTSO-E Transparency Platform")

# colour range fixed for the whole day so the hour slider is comparable
p_lo, p_hi = np.nanpercentile(day_df["price_eur_mwh"], [2, 98])
zmin, zmax = float(min(p_lo, 0)), float(max(p_hi, 1))

if metric == "Daily average":
    value = day_df.groupby("zone")["price_eur_mwh"].mean()
    map_title = "Daily average"
else:
    value = day_df[day_df["slot"] == hour].set_index("zone")["price_eur_mwh"]
    map_title = f"Price at {hour}"

# ---------------------------------------------------------------- tabs
tab_map, tab_matrix = st.tabs(["Map", "Matrix by region"])

# ---------------------------------------------------------------- map tab
st.session_state.setdefault("zones_sel", ["DK_1"])


def on_map_select():
    """Click on the map -> update the zone picker (shift-click adds zones)."""
    try:
        pts = st.session_state["map"]["selection"]["points"]
    except (KeyError, TypeError):
        return
    codes = []
    for p in pts:
        code = p.get("location") or p.get("id")
        if code in ZONE_NAME and code not in codes:
            codes.append(code)
    if codes:
        st.session_state["zones_sel"] = codes


with tab_map:
    left, right = st.columns([3, 2], gap="large")

    with left:
        geo_ids = [f["id"] for f in geo["features"]]
        selected = [z for z in st.session_state["zones_sel"] if z in geo_ids]
        fig = go.Figure(go.Choropleth(
            geojson=geo,
            locations=geo_ids,
            z=[value.get(z, np.nan) for z in geo_ids],
            customdata=[[ZONE_NAME[z], ZONE_REGION[z]] for z in geo_ids],
            colorscale=COLORSCALE, zmin=zmin, zmax=zmax,
            marker_line_color="white", marker_line_width=0.7,
            colorbar=dict(title=dict(text="EUR/MWh"), thickness=14, len=0.7),
            hovertemplate="<b>%{customdata[0]}</b> (%{location}, %{customdata[1]})<br>"
                          "%{z:.1f} EUR/MWh<extra></extra>",
            selectedpoints=[geo_ids.index(z) for z in selected] or None,
            selected=dict(marker=dict(opacity=1)),
            unselected=dict(marker=dict(opacity=0.5 if selected else 1)),
        ))
        fig.update_geos(
            scope="europe", projection_type="mercator", fitbounds="locations",
            showcountries=True, countrycolor="#9a9a9a", showland=True, landcolor="#dcdcdc",
            showocean=True, oceancolor="rgba(150,180,210,0.25)", showlakes=False,
            showframe=False, bgcolor="rgba(0,0,0,0)",
        )
        fig.update_layout(height=680, margin=dict(l=0, r=0, t=30, b=0), title=dict(text=map_title, x=0.01),
                          clickmode="event+select")
        st.plotly_chart(fig, key="map", on_select=on_map_select, selection_mode="points",
                        config={"displayModeBar": False})
        st.caption("Click a zone to see its prices (shift-click to compare several). "
                   "Grey = no spot market in the dataset. Sub-country zone borders (SE, NO, DK, IT) "
                   "follow county/region lines, so they are approximate.")

    with right:
        st.multiselect("Zones", ZONE_CODES, key="zones_sel", format_func=label,
                       help="Also updated by clicking the map.")
        chosen = st.session_state["zones_sel"]
        if not chosen:
            st.info("Select a zone on the map or in the box above.")
        else:
            sub = day_df[day_df["zone"].isin(chosen)]
            table = sub.pivot(index="slot", columns="zone", values="price_eur_mwh").reindex(slots)[
                [z for z in chosen if z in sub["zone"].unique()]]
            if table.empty:
                st.warning("No prices for the selected zone(s) on this day.")
            else:
                stats = pd.DataFrame({
                    "Average": table.mean(), "Min": table.min(), "Max": table.max(),
                    "Cheapest hour": table.idxmin(), "Priciest hour": table.idxmax(),
                }).rename_axis("Zone")
                st.dataframe(stats, column_config={
                    c: st.column_config.NumberColumn(format="%.1f") for c in ["Average", "Min", "Max"]})
                table.index.name = f"Hour ({day:%d %b})"
                st.dataframe(table, height=min(38 + 35 * len(table), 880),
                             column_config={z: st.column_config.NumberColumn(format="%.2f") for z in table.columns})
                st.download_button("Download CSV", table.to_csv().encode(),
                                   file_name=f"spot_prices_{day}.csv", mime="text/csv")
                line = go.Figure([go.Scatter(x=table.index, y=table[z], name=z, mode="lines+markers",
                                             line_shape="hv") for z in table.columns])
                line.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="EUR/MWh",
                                   legend=dict(orientation="h", y=1.15), hovermode="x unified")
                st.plotly_chart(line, key="line")

# ---------------------------------------------------------------- matrix tab
with tab_matrix:
    regions = st.multiselect("Regions", REGION_ORDER, default=[r for r in REGION_ORDER if r != "Other"])
    regions = [r for r in REGION_ORDER if r in regions]
    codes_by_region = {r: [z for z in ZONE_CODES if ZONE_REGION[z] == r] for r in regions}

    if not regions:
        st.info("Pick at least one region.")
    else:
        wide = day_df.pivot(index="zone", columns="slot", values="price_eur_mwh").reindex(columns=slots)

        summary = pd.DataFrame([
            {"Region": r, "Zones": len(c), "Average": wide.reindex(c).stack().mean(),
             "Min": wide.reindex(c).min().min(), "Max": wide.reindex(c).max().max()}
            for r, c in codes_by_region.items()]).set_index("Region")
        st.dataframe(summary, column_config={c: st.column_config.NumberColumn(format="%.1f")
                                             for c in ["Average", "Min", "Max"]})

        counts = [len(codes_by_region[r]) for r in regions]
        fig = make_subplots(rows=len(regions), cols=1, shared_xaxes=True, row_heights=counts,
                            vertical_spacing=0.012)
        for i, r in enumerate(regions, start=1):
            block = wide.reindex(codes_by_region[r])
            fig.add_trace(go.Heatmap(
                z=block.to_numpy(), x=slots, y=list(block.index), coloraxis="coloraxis",
                text=[[ZONE_NAME[z]] * len(slots) for z in block.index],
                texttemplate="%{z:.0f}", textfont=dict(size=11), xgap=1, ygap=1,
                hovertemplate="<b>%{y}</b> %{text}<br>%{x}: %{z:.2f} EUR/MWh<extra></extra>",
            ), row=i, col=1)
            fig.update_yaxes(title_text=f"<b>{r}</b>", autorange="reversed", row=i, col=1)
        fig.update_xaxes(side="top", row=1, col=1)
        fig.update_xaxes(type="category")
        fig.update_layout(
            height=120 + 27 * sum(counts) + 10 * len(regions),
            margin=dict(l=10, r=10, t=30, b=10),
            coloraxis=dict(colorscale=COLORSCALE, cmin=zmin, cmax=zmax,
                           colorbar=dict(title="EUR/MWh", thickness=14)),
        )
        st.plotly_chart(fig, key="matrix")
        st.caption("Cells show EUR/MWh for each hour of the delivery day (market time). "
                   "Colour scale is shared across regions; blank = not published for that zone/hour.")
        with st.expander("Raw matrix"):
            st.dataframe(wide.reindex([z for r in regions for z in codes_by_region[r]]).rename_axis("Zone"),
                         column_config={s: st.column_config.NumberColumn(format="%.1f") for s in slots})
