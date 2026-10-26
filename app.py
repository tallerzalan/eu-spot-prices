"""European day-ahead spot prices + imbalance - Streamlit dashboard (data from ENTSO-E)."""
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from zones import REGION_ORDER, ZONE_CODES, ZONE_NAME, ZONE_REGION

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("SPOT_DATA_DIR", ROOT / "data"))
MARKET_TZ = "Europe/Brussels"
COLORSCALE = "RdYlGn_r"  # green = cheap, red = expensive

st.set_page_config(page_title="European spot prices", page_icon=":zap:", layout="wide")


def label(code: str) -> str:
    return f"{code} - {ZONE_NAME[code]}"


def hour_label(h: int) -> str:
    return f"{h + 1} ({h:02d}:00)"  # delivery hour 1 = 00:00-01:00


# ---------------------------------------------------------------- data
@st.cache_data(show_spinner=False)
def load_prices(mtime: float) -> pd.DataFrame:  # mtime busts the cache when the CSV changes
    df = pd.read_csv(DATA / "prices.csv", parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(MARKET_TZ)
    df["date"] = df["time"].dt.date
    df["slot"] = df["time"].dt.strftime("%H:00")
    # on the autumn clock change the 02:00 hour occurs twice; keep the slots distinct
    df.loc[df.duplicated(["zone", "date", "slot"]), "slot"] += " (2)"
    return df.sort_values(["zone", "time"])


@st.cache_data(show_spinner=False)
def load_geo() -> dict:
    return json.loads((DATA / "zones.geojson").read_text(encoding="utf-8"))


@st.cache_data(show_spinner=False)
def load_imbalance(mtime: float, spot_mtime: float):
    """Hourly imbalance data joined with the spot price of the same zone and hour."""
    df = pd.read_csv(DATA / "imbalance.csv", parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    spot = pd.read_csv(DATA / "prices.csv", parse_dates=["time"])
    spot["time"] = pd.to_datetime(spot["time"], utc=True)
    df = df.merge(spot.rename(columns={"price_eur_mwh": "spot"}), on=["zone", "time"], how="left")
    df["price_avg"] = df[["price_long", "price_short"]].mean(axis=1)
    df["time"] = df["time"].dt.tz_convert(MARKET_TZ)
    df["date"] = df["time"].dt.date
    df["hour"] = df["time"].dt.hour
    return df.sort_values(["zone", "time"])


@st.cache_data(show_spinner=False)
def load_currency() -> dict:
    p = DATA / "imbalance_currency.json"
    return json.loads(p.read_text()) if p.exists() else {}


csv_path = DATA / "prices.csv"
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
    st.header("Spot price controls")
    day = st.selectbox("Delivery day", dates, index=dates.index(default_date),
                       format_func=lambda d: d.strftime("%a %d %b %Y"))
    day_df = prices[prices["date"] == day]
    slots = list(dict.fromkeys(day_df.sort_values("time")["slot"]))
    metric = st.radio("Map shows", ["Daily average", "Single hour"], horizontal=True)
    hour = None
    if metric == "Single hour":
        hour = st.select_slider("Hour (market time, CET/CEST)", options=slots, value=slots[min(12, len(slots) - 1)])
    show_labels = st.checkbox("Show prices on the map", value=True)
    st.caption(f"Zones with data this day: {day_df['zone'].nunique()} / {len(ZONE_CODES)}")
    st.caption(f"Latest price in dataset: {prices['time'].max():%a %d %b %H:%M} (market time)")

st.title("European power prices")
st.caption("Day-ahead spot in EUR/MWh (hourly means) and imbalance data · times in market time (CET/CEST) · "
           "source: ENTSO-E Transparency Platform")

# colour range fixed for the whole day so the hour slider is comparable
p_lo, p_hi = np.nanpercentile(day_df["price_eur_mwh"], [2, 98])
zmin, zmax = float(min(p_lo, 0)), float(max(p_hi, 1))

if metric == "Daily average":
    value = day_df.groupby("zone")["price_eur_mwh"].mean()
    map_title = f"Daily average - {day:%a %d %b %Y}"
else:
    value = day_df[day_df["slot"] == hour].set_index("zone")["price_eur_mwh"]
    map_title = f"Price at {hour} - {day:%a %d %b %Y}"

tab_map, tab_matrix, tab_imb = st.tabs(["Map", "Matrix by region", "Imbalance"])

# ---------------------------------------------------------------- map tab
st.session_state.setdefault("zones_sel", ["DK_1"])
st.session_state.setdefault("map_ver", 0)


def on_map_select():
    """A click on the map toggles that zone in the selection (shift-click selects several at once).

    The chart is re-created after every click (new key), so each click arrives as a fresh,
    single-point selection and clicking a selected zone again simply removes it.
    """
    key = f"map_{st.session_state['map_ver']}"
    try:
        pts = st.session_state[key]["selection"]["points"]
    except (KeyError, TypeError):
        return
    codes = []
    for p in pts:
        code = p.get("location") or p.get("id")
        if code in ZONE_NAME and code not in codes:
            codes.append(code)
    chosen = list(st.session_state["zones_sel"])
    if len(codes) > 1:
        chosen = codes
    elif codes:
        if codes[0] in chosen:
            chosen.remove(codes[0])
        else:
            chosen.append(codes[0])
    st.session_state["zones_sel"] = chosen
    st.session_state["map_ver"] += 1


with tab_map:
    geo_ids = [f["id"] for f in geo["features"]]
    selected = set(st.session_state["zones_sel"])
    vals = [value.get(z, np.nan) for z in geo_ids]

    fig = go.Figure(go.Choropleth(
        geojson=geo, locations=geo_ids, z=vals,
        customdata=[[ZONE_NAME[z], ZONE_REGION[z]] for z in geo_ids],
        colorscale=COLORSCALE, zmin=zmin, zmax=zmax,
        marker_line_color=["#111111" if z in selected else "white" for z in geo_ids],
        marker_line_width=[3.5 if z in selected else 0.7 for z in geo_ids],
        colorbar=dict(title=dict(text="EUR/MWh"), thickness=16, len=0.6, x=1.0),
        hovertemplate="<b>%{customdata[0]}</b> (%{location}, %{customdata[1]})<br>"
                      "%{z:.1f} EUR/MWh<br><i>click to add / remove</i><extra></extra>",
    ))
    if show_labels:
        props = {f["id"]: f["properties"] for f in geo["features"]}
        fig.add_trace(go.Scattergeo(
            lon=[props[z]["lon"] for z in geo_ids], lat=[props[z]["lat"] for z in geo_ids],
            text=[f"<b>{z}</b><br>{v:.0f}" if pd.notna(v) else f"{z}" for z, v in zip(geo_ids, vals)],
            mode="text", textfont=dict(size=12, color="#111111"), hoverinfo="skip", showlegend=False,
        ))
    fig.update_geos(
        scope="europe", projection_type="conic conformal", fitbounds="locations",
        showcountries=True, countrycolor="#9a9a9a", showland=True, landcolor="#dcdcdc",
        showocean=True, oceancolor="rgba(150,180,210,0.25)", showlakes=False,
        showframe=False, bgcolor="rgba(0,0,0,0)",
    )
    fig.update_layout(height=900, margin=dict(l=0, r=0, t=40, b=0), title=dict(text=map_title, x=0.01),
                      clickmode="event+select", dragmode="pan")
    st.plotly_chart(fig, key=f"map_{st.session_state['map_ver']}", on_select=on_map_select,
                    selection_mode="points", config={"displayModeBar": False, "scrollZoom": False})
    st.caption("Click a zone to add it to the selection below, click it again to remove it. "
               "Grey = no spot market in the dataset. Sub-country zone borders (SE, NO, DK, IT) follow "
               "county/region lines, so they are approximate.")

    # ---- data for the selected zones, underneath the map
    st.divider()
    st.subheader("Selected zones")
    top_l, top_r = st.columns([6, 1], vertical_alignment="bottom")
    with top_l:
        st.multiselect("Zones", ZONE_CODES, key="zones_sel", format_func=label,
                       help="Also updated by clicking the map.")
    with top_r:
        if st.button("Clear selection", use_container_width=True):
            st.session_state["zones_sel"] = []
            st.session_state["map_ver"] += 1
            st.rerun()

    chosen = st.session_state["zones_sel"]
    if not chosen:
        st.info("Click a zone on the map above, or pick one in the box, to see its prices.")
    else:
        sub = day_df[day_df["zone"].isin(chosen)]
        have = [z for z in chosen if z in sub["zone"].unique()]
        table = sub.pivot(index="slot", columns="zone", values="price_eur_mwh").reindex(slots)[have]
        missing = [z for z in chosen if z not in have]
        if missing:
            st.warning(f"No prices for {', '.join(missing)} on {day:%d %b}.")
        if not table.empty:
            stats = pd.DataFrame({
                "Name": [ZONE_NAME[z] for z in table.columns],
                "Average": table.mean(), "Min": table.min(), "Max": table.max(),
                "Cheapest hour": table.idxmin(), "Priciest hour": table.idxmax(),
            }).rename_axis("Zone")
            st.dataframe(stats, column_config={
                c: st.column_config.NumberColumn(format="%.1f") for c in ["Average", "Min", "Max"]})

            col_t, col_c = st.columns([2, 3], gap="large")
            with col_t:
                table.index.name = f"Hour ({day:%d %b})"
                st.dataframe(table, height=min(38 + 35 * len(table), 920),
                             column_config={z: st.column_config.NumberColumn(format="%.2f") for z in table.columns})
                st.download_button("Download CSV", table.to_csv().encode(),
                                   file_name=f"spot_prices_{day}.csv", mime="text/csv")
            with col_c:
                line = go.Figure([go.Scatter(x=table.index, y=table[z], name=z, mode="lines+markers",
                                             line_shape="hv") for z in table.columns])
                line.update_layout(height=520, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="EUR/MWh",
                                   legend=dict(orientation="h", y=1.08), hovermode="x unified")
                st.plotly_chart(line, key="line")

# ---------------------------------------------------------------- matrix tab
with tab_matrix:
    regions = st.multiselect("Regions", REGION_ORDER, default=REGION_ORDER)
    regions = [r for r in REGION_ORDER if r in regions]
    codes_by_region = {r: [z for z in ZONE_CODES if ZONE_REGION[z] == r] for r in regions}

    if not regions:
        st.info("Pick at least one region.")
    else:
        wide = day_df.pivot(index="zone", columns="slot", values="price_eur_mwh").reindex(columns=slots)
        ylabels = [f"{i + 1} ({s})" for i, s in enumerate(slots)]  # hour 1..24 downwards

        summary = pd.DataFrame([
            {"Region": r, "Zones": len(c), "Average": wide.reindex(c).stack().mean(),
             "Min": wide.reindex(c).min().min(), "Max": wide.reindex(c).max().max()}
            for r, c in codes_by_region.items()]).set_index("Region")
        st.dataframe(summary, column_config={c: st.column_config.NumberColumn(format="%.1f")
                                             for c in ["Average", "Min", "Max"]})

        fig = make_subplots(rows=1, cols=len(regions), shared_yaxes=True, horizontal_spacing=0.006,
                            column_widths=[len(codes_by_region[r]) for r in regions])
        for i, r in enumerate(regions, start=1):
            block = wide.reindex(codes_by_region[r]).T  # rows = hours, columns = zones
            fig.add_trace(go.Heatmap(
                z=block.to_numpy(), x=list(block.columns), y=ylabels, coloraxis="coloraxis",
                text=[[ZONE_NAME[z] for z in block.columns] for _ in ylabels],
                texttemplate="%{z:.0f}", textfont=dict(size=10), xgap=1, ygap=1,
                hovertemplate="<b>%{x}</b> %{text}<br>hour %{y}: %{z:.2f} EUR/MWh<extra></extra>",
            ), row=1, col=i)
            fig.update_xaxes(title_text=f"<b>{r}</b>", side="top", type="category", tickangle=-90,
                             row=1, col=i)
        fig.update_yaxes(autorange="reversed", type="category", categoryorder="array", categoryarray=ylabels)
        fig.update_layout(
            height=190 + 27 * len(slots), margin=dict(l=10, r=10, t=10, b=10),
            coloraxis=dict(colorscale=COLORSCALE, cmin=zmin, cmax=zmax,
                           colorbar=dict(title="EUR/MWh", thickness=14)),
        )
        st.plotly_chart(fig, key="matrix")
        st.caption("Cells show EUR/MWh. Rows are delivery hours (hour 1 = 00:00-01:00, market time), "
                   "columns are zones grouped by region. Colour scale is shared; blank = not published.")
        with st.expander("Raw matrix"):
            order = [z for r in regions for z in codes_by_region[r]]
            raw = wide.reindex(order).T
            raw.index = ylabels
            raw.columns = pd.MultiIndex.from_tuples([(ZONE_REGION[z], z) for z in raw.columns])
            st.dataframe(raw.round(1), height=min(38 + 35 * len(raw), 920))

# ---------------------------------------------------------------- imbalance tab
IMB_METRICS = {
    "Imbalance price - average of long & short": "price_avg",
    "Imbalance price - short (deficit position)": "price_short",
    "Imbalance price - long (surplus position)": "price_long",
    "Imbalance volume (MWh)": "volume_mwh",
    "Imbalance price minus spot price": "spread",
}
PRICE_COLS = {"price_avg", "price_short", "price_long", "spread"}


def color_args(col: str, series: pd.Series) -> dict:
    v = series.dropna()
    if v.empty:
        return dict(colorscale=COLORSCALE)
    lo, hi = np.nanpercentile(v, [2, 98])
    if col in ("volume_mwh", "spread"):
        m = float(max(abs(lo), abs(hi))) or 1.0
        return dict(colorscale="RdBu" if col == "volume_mwh" else "RdBu_r", zmin=-m, zmax=m, zmid=0)
    return dict(colorscale=COLORSCALE, zmin=float(lo), zmax=float(hi))


with tab_imb:
    imb_path = DATA / "imbalance.csv"
    if not imb_path.exists():
        st.info("No imbalance data yet. It is fetched by scripts/fetch_imbalance.py "
                "(the scheduled workflow runs it together with the spot prices).")
    else:
        imb = load_imbalance(imb_path.stat().st_mtime, csv_path.stat().st_mtime)
        cur = load_currency()
        avail = [z for z in ZONE_CODES if z in set(imb["zone"])]

        def zlabel(z):
            return f"{z} - {ZONE_NAME[z]} ({cur.get(z, '?')})"

        # imbalance minus spot only makes sense where both are in EUR
        imb.loc[imb["zone"].map(lambda z: cur.get(z) != "EUR"), "spot"] = np.nan
        imb["spread"] = imb["price_avg"] - imb["spot"]

        st.caption("Imbalance = the difference between what balance-responsible parties scheduled and what they "
                   "actually used or produced. **Long / short price**: what a surplus / deficit position is settled "
                   "at (single-price systems have one price, shown for both). **Volume**: net system imbalance, "
                   "positive = surplus, negative = deficit. Prices are in each country's own currency "
                   "(shown in brackets); imbalance-minus-spot is only computed for EUR zones. "
                   "Hourly values: prices are means, volumes are sums.")

        c1, c2, c3 = st.columns([3, 2, 1])
        default_zones = [z for z in ["DE_LU", "NL", "DK_1", "BE", "FR"] if z in avail][:2] or avail[:1]
        st.session_state.setdefault("imb_zones", default_zones)
        with c1:
            zones_i = st.multiselect("Zones", avail, key="imb_zones", format_func=zlabel)
        with c2:
            m_name = st.selectbox("Metric", list(IMB_METRICS))
        with c3:
            days = st.select_slider("Past days", [7, 14, 21, 30], value=30)
        col = IMB_METRICS[m_name]

        last = imb["date"].max()
        win = imb[imb["date"] > last - pd.Timedelta(days=days)]
        st.caption(f"Data from {win['date'].min():%d %b} to {last:%d %b %Y} · "
                   f"{len(avail)} zones publish imbalance data.")

        def unit_of(zs):
            if col == "volume_mwh":
                return "MWh"
            cs = {cur.get(z, "?") for z in zs}
            return f"{cs.pop()}/MWh" if len(cs) == 1 else "local currency/MWh"

        # ---- A: all zones x days
        st.subheader("All zones, day by day")
        hsel = st.selectbox("Hour of day", ["All hours (daily average)"] + [hour_label(h) for h in range(24)])
        sub = win if hsel.startswith("All") else win[win["hour"] == int(hsel.split(" ")[0]) - 1]
        piv = sub.pivot_table(index="zone", columns="date", values=col, aggfunc="mean")
        piv = piv.reindex([z for z in ZONE_CODES if z in piv.index])
        if piv.empty or piv.notna().sum().sum() == 0:
            st.info("No data for this metric.")
        else:
            if col in PRICE_COLS and col != "spread" and len({cur.get(z) for z in piv.index}) > 1:
                st.warning("Zones are in different currencies - compare colours within a currency only "
                           "(EUR zones are the majority).")
            hm = go.Figure(go.Heatmap(
                z=piv.to_numpy(), x=[d.strftime("%d %b") for d in piv.columns], y=list(piv.index),
                text=[[ZONE_NAME[z]] * piv.shape[1] for z in piv.index], xgap=1, ygap=1,
                texttemplate="%{z:.0f}", textfont=dict(size=9),
                hovertemplate="<b>%{y}</b> %{text}<br>%{x}: %{z:.2f}<extra></extra>",
                colorbar=dict(title=unit_of(piv.index), thickness=14), **color_args(col, sub[col]),
            ))
            hm.update_yaxes(autorange="reversed", type="category")
            hm.update_xaxes(side="top", type="category")
            hm.update_layout(height=140 + 24 * len(piv), margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(hm, key="imb_matrix")

        if not zones_i:
            st.info("Pick at least one zone above for the trend views.")
        else:
            zsub = win[win["zone"].isin(zones_i)]

            # ---- B: time series
            st.subheader(f"{m_name} - trend")
            overlay = False
            if col in PRICE_COLS and col != "spread":
                overlay = st.checkbox("Overlay day-ahead spot price (dashed, EUR zones)", value=True)
            ts = go.Figure()
            palette = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2"]
            for i, z in enumerate(zones_i):
                d = zsub[zsub["zone"] == z]
                colr = palette[i % len(palette)]
                if col == "volume_mwh" and len(zones_i) == 1:
                    ts.add_trace(go.Bar(x=d["time"], y=d[col], name=z,
                                        marker_color=np.where(d[col] >= 0, "#2b6cb0", "#c53030")))
                else:
                    ts.add_trace(go.Scatter(x=d["time"], y=d[col], name=z, mode="lines", line=dict(color=colr, width=1.4)))
                if overlay and cur.get(z) == "EUR":
                    ts.add_trace(go.Scatter(x=d["time"], y=d["spot"], name=f"{z} spot", mode="lines",
                                            line=dict(color=colr, width=1, dash="dot"), opacity=0.8))
            ts.update_layout(height=460, margin=dict(l=0, r=0, t=10, b=0), yaxis_title=unit_of(zones_i),
                             hovermode="x unified", legend=dict(orientation="h", y=1.1))
            st.plotly_chart(ts, key="imb_ts")

            # ---- C + D: hour-of-day profile and day x hour heatmap
            left, right = st.columns(2, gap="large")
            with left:
                st.subheader("Average by hour of day")
                prof = go.Figure()
                for i, z in enumerate(zones_i):
                    g = zsub[zsub["zone"] == z].groupby("hour")[col].mean().reindex(range(24))
                    prof.add_trace(go.Scatter(x=[hour_label(h) for h in g.index], y=g.values, name=z, mode="lines+markers",
                                              line=dict(color=palette[i % len(palette)])))
                prof.update_xaxes(type="category", tickangle=-60)
                prof.update_layout(height=430, margin=dict(l=0, r=0, t=10, b=0), yaxis_title=unit_of(zones_i),
                                   legend=dict(orientation="h", y=1.1))
                st.plotly_chart(prof, key="imb_prof")
            with right:
                zh = st.selectbox("Zone for the hour x day heatmap", zones_i, format_func=zlabel)
                st.subheader(f"{zh}: hour x day")
                d = zsub[zsub["zone"] == zh]
                hp = d.pivot_table(index="hour", columns="date", values=col, aggfunc="mean").reindex(range(24))
                hh = go.Figure(go.Heatmap(
                    z=hp.to_numpy(), x=[x.strftime("%d %b") for x in hp.columns], y=[hour_label(h) for h in hp.index],
                    xgap=1, ygap=1, colorbar=dict(title=unit_of([zh]), thickness=12),
                    hovertemplate="%{x}, hour %{y}: %{z:.2f}<extra></extra>", **color_args(col, d[col]),
                ))
                hh.update_yaxes(autorange="reversed", type="category")
                hh.update_xaxes(type="category", tickangle=-60)
                hh.update_layout(height=430, margin=dict(l=0, r=0, t=10, b=0))
                st.plotly_chart(hh, key="imb_hh")

            # ---- E: summary table
            st.subheader("Summary over the selected period")
            rows = []
            for z in zones_i:
                d = zsub[zsub["zone"] == z]
                v = d["volume_mwh"].dropna()
                rows.append({
                    "Zone": z, "Currency": cur.get(z, "?"),
                    "Avg price": d["price_avg"].mean(), "Avg short price": d["price_short"].mean(),
                    "Avg long price": d["price_long"].mean(), "Avg spot (EUR)": d["spot"].mean(),
                    "Avg imbalance - spot": d["spread"].mean(),
                    "Avg volume (MWh/h)": v.mean() if len(v) else np.nan,
                    "Hours in surplus %": (v > 0).mean() * 100 if len(v) else np.nan,
                    "Hours in deficit %": (v < 0).mean() * 100 if len(v) else np.nan,
                    "Hours with data": len(d),
                })
            st.dataframe(pd.DataFrame(rows).set_index("Zone"), column_config={
                c: st.column_config.NumberColumn(format="%.1f") for c in
                ["Avg price", "Avg short price", "Avg long price", "Avg spot (EUR)", "Avg imbalance - spot",
                 "Avg volume (MWh/h)", "Hours in surplus %", "Hours in deficit %"]})
            st.download_button("Download selected imbalance data (CSV)",
                               zsub.drop(columns=["date", "hour"]).to_csv(index=False).encode(),
                               file_name="imbalance_selected.csv", mime="text/csv")
