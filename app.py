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

# Power-market products as blocks of delivery hours (market time, hour 0 = 00:00-01:00).
# (first hour, last hour + 1). Edit here to change the definitions everywhere.
PRODUCTS = {
    "Base": [(0, 24)],
    "Peak": [(8, 20)],  # 08:00-20:00, the standard peak block
    "Off-peak 1": [(0, 8)],  # night: 00:00-08:00
    "Off-peak 2": [(20, 24)],  # late evening: 20:00-24:00
    "Off-peak": [(0, 8), (20, 24)],  # off-peak 1 + 2 together
    "Morning shoulder": [(6, 9)],  # 06:00-09:00
    "Evening shoulder": [(17, 20)],  # 17:00-20:00
}
PRODUCT_HELP = ("**Base** = all 24 hours (the daily average) · **Peak** = 08:00-20:00 · "
                "**Off-peak 1** = 00:00-08:00 · **Off-peak 2** = 20:00-24:00 · **Off-peak** = both off-peak blocks · "
                "**Morning shoulder** = 06:00-09:00 · **Evening shoulder** = 17:00-20:00. "
                "Market time (CET/CEST); simple averages of the prices in each block. "
                "The peak product is formally a weekday product - weekend values use the same hours.")


def product_means(df: pd.DataFrame, col: str = "price_eur_mwh") -> pd.DataFrame:
    """zone x product table of average `col` over each block of hours (df needs zone + hour columns)."""
    out = {}
    for name, ranges in PRODUCTS.items():
        mask = np.zeros(len(df), dtype=bool)
        for a, b in ranges:
            mask |= ((df["hour"] >= a) & (df["hour"] < b)).to_numpy()
        out[name] = df[mask].groupby("zone")[col].mean()
    return pd.DataFrame(out)

st.set_page_config(page_title="European spot prices", page_icon=":zap:", layout="wide")

LEGACY_CURRENCY = {"HU": ("HUF", "2026-10-01")}  # zone: (currency used before the switch date, switch date)
MAP_PROJECTION = "equirectangular"  # stretches Europe to fill the page width; "natural earth" is less distorted
MAP_LON, MAP_LAT = [-10.5, 31.5], [35.5, 71.5]  # zoom: just the zones, no Russia / Africa / Turkey
MAP_HEIGHT = 900


def label(code: str) -> str:
    return f"{code} - {ZONE_NAME[code]}"


# ---- granularity: hourly or quarter-hourly (15-minute) data for the whole dashboard
FILES = {"Hourly": ("prices.csv", "imbalance.csv"), "15-minute": ("prices_15min.csv", "imbalance_15min.csv")}
with st.sidebar:
    st.header("Controls")
    _avail = [g for g, (pf, _) in FILES.items() if (DATA / pf).exists()]
    gran = st.segmented_control("Granularity", list(FILES), default="Hourly", key="gran",
                                help="Hourly = hourly means. 15-minute = the native settlement periods "
                                     "(day-ahead has been 15-minute since 1 Oct 2025).")
    if gran is None:
        gran = "Hourly"
    if gran not in _avail:
        st.warning("No 15-minute data yet - the next workflow run creates it. Showing hourly data.")
        gran = "Hourly"
QH = gran == "15-minute"
NP = 96 if QH else 24  # delivery periods per day
PER = "15-minute period" if QH else "hour"
PRICES_FILE, IMB_FILE = FILES[gran]


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def hour_label(i: int) -> str:
    """Product name of delivery period i (0-based) within the day, e.g. H1Q2 00:15-00:30 or H13 12:00-13:00.

    H1 = first delivery hour (00:00-01:00); Q1-Q4 = the quarters inside that hour.
    """
    if QH:
        return f"H{i // 4 + 1}Q{i % 4 + 1} {_hhmm(i * 15)}-{_hhmm(i * 15 + 15)}"
    return f"H{i + 1} {_hhmm(i * 60)}-{_hhmm(i * 60 + 60)}"


def slot_label(slot: str) -> str:
    """hour_label for a 'HH:MM' slot string of the price data (keeps the ' (2)' of the repeated DST hour)."""
    i = (int(slot[:2]) * 60 + int(slot[3:5])) // (15 if QH else 60)
    return hour_label(i) + (" (2)" if slot.endswith("(2)") else "")


def period_index(t: pd.Series) -> pd.Series:
    return t.dt.hour * 4 + t.dt.minute // 15 if QH else t.dt.hour


# ---------------------------------------------------------------- data
@st.cache_data(show_spinner=False)
def load_prices(mtime: float, filename: str) -> pd.DataFrame:  # mtime busts the cache when the CSV changes
    df = pd.read_csv(DATA / filename, parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(MARKET_TZ)
    df["date"] = df["time"].dt.date
    df["hour"] = df["time"].dt.hour
    df["slot"] = df["time"].dt.strftime("%H:%M")
    # on the autumn clock change the 02:00 hour occurs twice; keep the slots distinct
    df.loc[df.duplicated(["zone", "date", "slot"]), "slot"] += " (2)"
    return df.sort_values(["zone", "time"])


@st.cache_data(show_spinner=False)
def load_geo() -> dict:
    return json.loads((DATA / "zones.geojson").read_text(encoding="utf-8"))


@st.cache_data(show_spinner=False)
def load_imbalance(mtime: float, spot_mtime: float, imb_file: str, prices_file: str):
    """Imbalance data in EUR, joined with spot. Returns (dataframe, original currency per zone).

    - prices quoted in another currency (CZK, PLN, RON, ...) are converted with the ECB reference rate
      of the same day (data/fx.csv, forward-filled over weekends); nothing is dropped, however extreme
    - zones whose "short" price is always ~0 publish a single price in the "long" column -> use it for both
    - zones with no currency tag get EUR if their prices sit at the same level as spot
    price_sys = the price that applied in the direction the system was out of balance:
    price_short (upregulation) when the system was short, price_long (downregulation) when it was long.
    """
    df = pd.read_csv(DATA / imb_file, parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    spot = pd.read_csv(DATA / prices_file, parse_dates=["time"])
    spot["time"] = pd.to_datetime(spot["time"], utc=True)
    df = df.merge(spot.rename(columns={"price_eur_mwh": "spot"}), on=["zone", "time"], how="left")

    p = DATA / "imbalance_currency.json"
    cur = json.loads(p.read_text()) if p.exists() else {}
    for z, g in df.groupby("zone"):
        if cur.get(z) in (None, "?"):
            ratio = (g["price_long"] / g["spot"]).replace([np.inf, -np.inf], np.nan).median()
            cur[z] = "EUR" if pd.notna(ratio) and 0.3 < ratio < 3 else "?"

    short_unused = (df["price_short"].abs() < 0.01).groupby(df["zone"]).transform("mean") > 0.9
    df.loc[short_unused, "price_short"] = df.loc[short_unused, "price_long"]

    # ---- to EUR
    day = df["time"].dt.tz_convert(MARKET_TZ).dt.tz_localize(None).dt.normalize()
    fx_path = DATA / "fx.csv"
    fx = pd.read_csv(fx_path, parse_dates=["date"]).pivot(index="date", columns="currency", values="per_eur")         if fx_path.exists() else pd.DataFrame()
    if len(fx):
        fx = fx.reindex(pd.date_range(min(fx.index.min(), day.min()), max(fx.index.max(), day.max()))).ffill().bfill()
    rate = pd.Series(1.0, index=df.index)
    for z, c in cur.items():
        if c == "EUR":
            continue
        m = df["zone"] == z
        rate[m] = day[m].map(fx[c]) if c in fx.columns else np.nan
    # Hungary quoted imbalance prices in HUF until 30 Sep 2026 and in EUR from 1 Oct 2026 (tagged EUR today)
    for z, (old_cur, switch) in LEGACY_CURRENCY.items():
        m = (df["zone"] == z) & (day < pd.Timestamp(switch))
        if old_cur in fx.columns:
            rate[m] = day[m].map(fx[old_cur])
    for c in ("price_long", "price_short"):
        df[c] = df[c] / rate

    df["price_avg"] = df[["price_long", "price_short"]].mean(axis=1)
    v = df["volume_mwh"]
    df["price_sys"] = np.where(v < 0, df["price_short"], np.where(v > 0, df["price_long"], df["price_avg"]))
    df["price_sys"] = df["price_sys"].fillna(df["price_avg"])
    df["premium"] = df["price_sys"] - df["spot"]
    df["spot"] = df["spot"].where(df["price_sys"].notna())

    df["time"] = df["time"].dt.tz_convert(MARKET_TZ)
    df["date"] = df["time"].dt.date
    df["hour"] = df["time"].dt.hour
    df["slot_i"] = df["time"].dt.hour * 4 + df["time"].dt.minute // 15  # 0..95; folded to hours by the caller
    return df.sort_values(["zone", "time"]), cur


csv_path = DATA / PRICES_FILE
if not csv_path.exists():
    st.error(f"data/{PRICES_FILE} not found - run scripts/fetch_prices.py first.")
    st.stop()
prices = load_prices(csv_path.stat().st_mtime, PRICES_FILE)
geo = load_geo()

# ---------------------------------------------------------------- sidebar
dates = sorted(prices["date"].unique(), reverse=True)
coverage = prices.groupby("date")["zone"].nunique()
default_date = next((d for d in dates if coverage[d] >= 0.5 * len(ZONE_CODES)), dates[0])

with st.sidebar:
    day = st.selectbox("Delivery day", dates, index=dates.index(default_date),
                       format_func=lambda d: d.strftime("%a %d %b %Y"))
    day_df = prices[prices["date"] == day]
    slots = list(dict.fromkeys(day_df.sort_values("time")["slot"]))
    metric = st.selectbox("Map shows", list(PRODUCTS) + ["Single time slot"],
                          help="Base = daily average. Peak, off-peak and shoulder blocks are defined below the map.")
    hour = None
    if metric == "Single time slot":
        hour = st.select_slider(f"Start of {PER} (market time, CET/CEST)", options=slots, value=slots[min(12, len(slots) - 1)])
    show_labels = st.checkbox("Show prices on the map", value=True)
    st.caption(f"Zones with data this day: {day_df['zone'].nunique()} / {len(ZONE_CODES)}")
    st.caption(f"Latest price in dataset: {prices['time'].max():%a %d %b %H:%M} (market time)")

st.title("European power prices")
st.caption(f"Day-ahead spot in EUR/MWh ({'15-minute prices' if QH else 'hourly means'}) and imbalance data · times in market time (CET/CEST) · "
           "source: ENTSO-E Transparency Platform")

# colour range fixed for the whole day so the hour slider is comparable
p_lo, p_hi = np.nanpercentile(day_df["price_eur_mwh"], [2, 98])
zmin, zmax = float(min(p_lo, 0)), float(max(p_hi, 1))

day_stats = product_means(day_df)  # zone x product (Base, Peak, Off-peak 1, ...)
if metric != "Single time slot":
    value = day_stats[metric]
    map_title = f"{metric} - {day:%a %d %b %Y}"
else:
    value = day_df[day_df["slot"] == hour].set_index("zone")["price_eur_mwh"]
    map_title = f"Price at {hour} - {day:%a %d %b %Y}"

tab_map, tab_matrix, tab_imb, tab_reg = st.tabs(["Map", "Matrix by region", "Imbalance", "Regulation"])

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

    # grey underlay so zones without data stay visible; the surroundings (Russia, Africa, ...) are not drawn
    fig = go.Figure(go.Choropleth(
        geojson=geo, locations=geo_ids, z=[0] * len(geo_ids), showscale=False, hoverinfo="skip",
        colorscale=[[0, "#cfd3d8"], [1, "#cfd3d8"]], marker_line_color="white", marker_line_width=0.7,
    ))
    fig.add_trace(go.Choropleth(
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
        scope="europe", projection_type=MAP_PROJECTION, visible=False, bgcolor="rgba(0,0,0,0)",
        lonaxis_range=MAP_LON, lataxis_range=MAP_LAT,
    )
    fig.update_layout(height=MAP_HEIGHT, margin=dict(l=0, r=0, t=40, b=0), title=dict(text=map_title, x=0.01),
                      clickmode="event+select", dragmode="pan")
    st.plotly_chart(fig, key=f"map_{st.session_state['map_ver']}", on_select=on_map_select,
                    selection_mode="points", config={"displayModeBar": False, "scrollZoom": False})
    st.caption("Click a zone to add it to the selection below, click it again to remove it. "
               "Light grey = no spot price for that zone and day. Sub-country zone borders (SE, NO, DK, IT) follow "
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
            stats = day_stats.reindex(list(table.columns))
            stats.insert(0, "Name", [ZONE_NAME[z] for z in stats.index])
            stats.index.name = "Zone"
            st.dataframe(stats, column_config={c: st.column_config.NumberColumn(format="%.1f") for c in PRODUCTS})
            st.caption(PRODUCT_HELP)

            col_t, col_c = st.columns([2, 3], gap="large")
            with col_t:
                table.index.name = f"{'Time' if QH else 'Hour'} ({day:%d %b})"
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
        ylabels = [slot_label(sl) for sl in slots]  # H1..H24 (or H1Q1..H24Q4) downwards

        zstats = day_stats.reindex([z for r in regions for z in codes_by_region[r]])
        zstats.insert(0, "Region", [ZONE_REGION[z] for z in zstats.index])
        zstats.insert(1, "Name", [ZONE_NAME[z] for z in zstats.index])
        zstats.index.name = "Zone"
        summary = zstats.drop(columns="Name").groupby("Region", sort=False).mean()
        summary.insert(0, "Zones", zstats.groupby("Region", sort=False).size())
        pcfg = {c: st.column_config.NumberColumn(format="%.1f") for c in PRODUCTS}
        st.markdown("**By region** (simple average of the zones' values)")
        st.dataframe(summary, column_config=pcfg)
        with st.expander("Statistics by zone", expanded=False):
            st.dataframe(zstats, column_config=pcfg, height=min(38 + 35 * len(zstats), 700))
            st.caption(PRODUCT_HELP)

        fig = make_subplots(rows=1, cols=len(regions), shared_yaxes=True, horizontal_spacing=0.006,
                            column_widths=[len(codes_by_region[r]) for r in regions])
        for i, r in enumerate(regions, start=1):
            block = wide.reindex(codes_by_region[r]).T  # rows = hours, columns = zones
            fig.add_trace(go.Heatmap(
                z=block.to_numpy(), x=list(block.columns), y=ylabels, coloraxis="coloraxis",
                text=[[ZONE_NAME[z] for z in block.columns] for _ in ylabels],
                texttemplate="%{z:.0f}", textfont=dict(size=9 if QH else 10), xgap=1, ygap=1,
                hovertemplate="<b>%{x}</b> %{text}<br>%{y}: %{z:.2f} EUR/MWh<extra></extra>",
            ), row=1, col=i)
            fig.update_xaxes(title_text=f"<b>{r}</b>", side="top", type="category", tickangle=-90,
                             row=1, col=i)
        fig.update_yaxes(autorange="reversed", type="category", categoryorder="array", categoryarray=ylabels)
        fig.update_layout(
            height=190 + (20 if QH else 27) * len(slots), margin=dict(l=10, r=10, t=10, b=10),
            coloraxis=dict(colorscale=COLORSCALE, cmin=zmin, cmax=zmax,
                           colorbar=dict(title="EUR/MWh", thickness=14)),
        )
        st.plotly_chart(fig, key="matrix")
        st.caption("Cells show EUR/MWh. Rows are delivery products (H1 = 00:00-01:00, H1Q1 = 00:00-00:15, market time), "
                   "columns are zones grouped by region. Colour scale is shared; blank = not published.")
        with st.expander("Raw matrix"):
            order = [z for r in regions for z in codes_by_region[r]]
            raw = wide.reindex(order).T
            raw.index = ylabels
            raw.columns = pd.MultiIndex.from_tuples([(ZONE_REGION[z], z) for z in raw.columns])
            st.dataframe(raw.round(1), height=min(38 + 35 * len(raw), 920))


# ---------------------------------------------------------------- imbalance tab
IMB_METRICS = {
    "Imbalance price": "price_sys",
    "Premium: imbalance price minus spot": "premium",
    "Imbalance volume (MWh)": "volume_mwh",
    "Upregulation price (system short)": "price_short",
    "Downregulation price (system long)": "price_long",
}
IMB_HELP = """
**Colours:** green = bull, red = bear. A high imbalance price is green, a low or negative one red. Premium above
spot is green, below spot is red. Upregulation is green, downregulation is red. Rows are named by product:
H13 = 12:00-13:00, H13Q2 = 12:15-12:30. Hungary quoted HUF until 30 Sep 2026 and EUR from 1 Oct; the earlier days
are converted at the ECB rate.

**Imbalance** - the grid is in balance when the electricity produced equals the electricity consumed in real time.
When it does not, the grid operator (TSO) settles the difference at an *imbalance price*.

- **Positive imbalance (surplus, system long, downregulation, red)** - supply exceeds demand, e.g. a spike in wind or
  solar or a sudden drop in demand. Too much surplus pushes the grid frequency up and can overload equipment.
  The TSO pays to scale generation back or demand up.
- **Negative imbalance (deficit, system short, upregulation, green)** - demand exceeds supply, e.g. a large plant
  trips or consumers use more than forecast. A deficit pulls the frequency down and risks outages. The TSO pays to
  ramp generation up or demand down.

**Imbalance price** - the rate the TSO applies to settle the gap between what a balance-responsible party (BRP)
planned and what it actually injected or withdrew in a settlement period. This is the number to compare with spot.

- In a **short system** (power deficit, upregulation, bull, green) the TSO has to buy extra power, so the
  imbalance price is high. A BRP that is **short** too (took out more or injected less than planned) makes the
  deficit worse and is **penalised**: it buys the missing energy at the high imbalance price. A BRP that is **long**
  (injected more or took out less than planned) helps the grid and is **rewarded**: its surplus is bought at that
  high price.
- In a **long system** (power surplus, downregulation, bear, red) the TSO pays to scale back, so the imbalance
  price is low and can drop below zero in extreme over-supply. A BRP that is **long** too (produced too much or
  consumed too little) makes the surplus worse and is **penalised**: it sells its excess at the low or negative
  price, i.e. it may pay to dump power into a congested grid. A BRP that is **short** helps the grid and is
  **rewarded**: it buys the missing energy at that low price.
- A BRP is never penalised for helping the grid (short while the system is long, long while it is short). In
  dual-price countries (e.g. FR, NL, ES, PT) the helping party and the penalised party settle at different prices;
  in single-price countries everybody settles at the same one.

**What the tab shows**

- **Imbalance price** - the price of the direction the system was actually out of balance: the upregulation price
  in a deficit hour, the downregulation price in a surplus hour (single-price countries have only one price).
- **Premium** - imbalance price minus the day-ahead spot price of the same hour.
  Positive (green, bull) = settling the imbalance was more expensive than buying on the day-ahead auction.
  Negative (red, bear) = it was cheaper than spot.
- **Volume** - net imbalance of the whole zone in MWh. Positive = surplus = downregulation = red (bear).
  Negative = deficit = upregulation = green (bull).
- **Upregulation / downregulation price** - the two raw prices. The upregulation price (ENTSO-E "short") applies
  in hours where the system needs more power, the downregulation price (ENTSO-E "long") where it needs less.
  A party that helps the grid (short while the system is long, long while it is short) is not penalised; a party that
  worsens the imbalance settles at the unfavourable price. The two only differ in dual-price countries
  (e.g. FR, NL, ES, PT).

Granularity: the toggle in the sidebar switches the whole dashboard between hourly means and 15-minute values
(hourly volumes are sums). Zones that publish coarser than 15 minutes are spread evenly over the quarters.
All prices are in EUR/MWh. Zones that quote another currency (CZ, PL, RO) are converted with the ECB euro
foreign exchange reference rate of the same day. The ECB publishes these rates around 16:00 CET on every TARGET
business day (public feed: ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml, no key needed); weekends and
holidays use the last published rate. No price is dropped, however high or low. Price colours are capped at the
2nd/98th percentile so outliers do not wash out the scale; the values themselves are untouched.
"""


BULL, BEAR = "#2f9e44", "#e03131"  # green = bull (above spot / upregulation), red = bear (below spot / downregulation)


def color_args(col: str, series: pd.Series) -> dict:
    v = series.dropna()
    if v.empty:
        return dict(colorscale="RdYlGn")
    lo, hi = np.nanpercentile(v, [2, 98])
    if col in ("volume_mwh", "premium"):
        m = float(max(abs(lo), abs(hi))) or 1.0
        # premium: green above spot, red below. volume: surplus (+) = downregulation = red, deficit (-) = green
        return dict(colorscale="RdYlGn" if col == "premium" else "RdYlGn_r", zmin=-m, zmax=m, zmid=0)
    return dict(colorscale="RdYlGn", zmin=float(lo), zmax=float(hi))  # high price = green (bull), low = red (bear)


def spot_vs_imbalance(x, spot, imbp, prem, height=470):
    f = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.62, 0.38], vertical_spacing=0.05)
    f.add_trace(go.Scatter(x=x, y=spot, name="Spot (day-ahead)", mode="lines",
                           line=dict(color="#868e96", width=1.6)), row=1, col=1)
    f.add_trace(go.Scatter(x=x, y=imbp, name="Imbalance price", mode="lines",
                           line=dict(color="#1c1c1c", width=1.6)), row=1, col=1)
    f.add_trace(go.Bar(x=x, y=prem, name="Premium", showlegend=False,
                       marker_color=np.where(np.nan_to_num(np.asarray(prem, dtype=float)) >= 0, BULL, BEAR)),
                row=2, col=1)
    f.update_yaxes(title_text="EUR/MWh", row=1, col=1)
    f.update_yaxes(title_text="Premium (EUR/MWh)", row=2, col=1)
    f.update_layout(height=height, margin=dict(l=0, r=0, t=10, b=0), hovermode="x unified",
                    legend=dict(orientation="h", y=1.1))
    return f


with tab_imb:
    imb_path = DATA / IMB_FILE
    if not imb_path.exists():
        st.info("No imbalance data yet. It is fetched by scripts/fetch_imbalance.py "
                "(the scheduled workflow runs it together with the spot prices).")
    else:
        imb, cur = load_imbalance(imb_path.stat().st_mtime, csv_path.stat().st_mtime, IMB_FILE, PRICES_FILE)
        avail = [z for z in ZONE_CODES if z in set(imb["zone"])]

        def zlabel(z):
            return f"{z} - {ZONE_NAME[z]}"

        c1, c2, c3 = st.columns([2, 2, 1])
        default_zone = next((z for z in ["NL", "FR", "BE", "DK_1", "SE_3"] if z in avail), avail[0])
        with c1:
            zone = st.selectbox("Zone", avail, index=avail.index(default_zone), format_func=zlabel)
        with c2:
            m_name = st.selectbox("Metric", list(IMB_METRICS))
        with c3:
            days = st.select_slider("Past days", [7, 14, 21, 30], value=30)
        with st.expander("How to read this"):
            st.markdown(IMB_HELP)

        col = IMB_METRICS[m_name]
        last = imb["date"].max()
        win = imb[imb["date"] > last - pd.Timedelta(days=days)]
        zd = win[win["zone"] == zone]
        unit = "MWh" if col == "volume_mwh" else "EUR/MWh"
        st.caption(f"{zlabel(zone)} · {win['date'].min():%d %b} - {last:%d %b %Y} (market time) · "
                   f"{len(avail)} zones publish imbalance data."
                   + (f" · {zone} is quoted in {cur[zone]}, converted to EUR at the daily ECB rate."
                      if cur.get(zone) not in ("EUR", None) else ""))

        # ---- hour x day heatmap (newest day first)
        st.subheader(f"{zone}: {m_name.split(':')[0].lower()} by {PER} and day")
        if zd[col].notna().sum() == 0:
            st.info(f"{zone} has no {m_name.lower()} data in this period.")
        else:
            hp = zd.pivot_table(index="slot_i" if QH else "hour", columns="date", values=col, aggfunc="mean").reindex(range(NP))
            newest_first = sorted(hp.columns, reverse=True)
            hp = hp[newest_first]
            hm = go.Figure(go.Heatmap(
                z=hp.to_numpy(), x=[d.strftime("%a %d %b") for d in newest_first], y=[hour_label(h) for h in range(NP)],
                xgap=1, ygap=1, texttemplate="%{z:.0f}", textfont=dict(size=8 if QH else 9),
                colorbar=dict(title=unit, thickness=14),
                hovertemplate="%{x}, %{y}: %{z:.2f}<extra></extra>", **color_args(col, zd[col]),
            ))
            hm.update_yaxes(autorange="reversed", type="category")
            hm.update_xaxes(type="category", side="top", tickangle=-90)
            hm.update_layout(height=190 + (20 if QH else 25) * NP, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(hm, key="imb_hm")

        # ---- spot -> imbalance premium
        st.subheader("Spot to imbalance: the premium")
        if zd["price_sys"].notna().sum() == 0 or zd["spot"].notna().sum() == 0:
            st.info("The premium needs both an imbalance price and a spot price for this zone.")
        else:
            left, right = st.columns(2, gap="large")
            with left:
                st.markdown("**Over time**")
                st.plotly_chart(spot_vs_imbalance(zd["time"], zd["spot"], zd["price_sys"], zd["premium"]),
                                key="imb_t")
            with right:
                st.markdown(f"**Average by {PER} of the day**")
                g = zd.groupby("slot_i" if QH else "hour")[["spot", "price_sys", "premium"]].mean().reindex(range(NP))
                fh = spot_vs_imbalance([hour_label(h) for h in g.index], g["spot"], g["price_sys"], g["premium"])
                fh.update_xaxes(type="category", tickangle=-60)
                st.plotly_chart(fh, key="imb_h")

            zm = zd[zd["premium"].notna()]  # only hours that have both a spot and an imbalance price
            tbl = pd.DataFrame({
                "Spot": product_means(zm, "spot").loc[zone],
                "Imbalance price": product_means(zm, "price_sys").loc[zone],
                "Premium": product_means(zm, "premium").loc[zone],
            }).T
            st.markdown(f"**{zone} by product** (EUR/MWh, average over the {len(zm)} {PER}s with both prices)")
            st.dataframe(tbl, column_config={c: st.column_config.NumberColumn(format="%.1f") for c in PRODUCTS})

        # ---- compare zones + summary
        st.subheader("Compare zones")
        others = st.multiselect("Add zones to compare", [z for z in avail if z != zone], format_func=zlabel)
        zs = [zone] + others
        zsub = win[win["zone"].isin(zs)]
        if others:
            palette = ["#1f77b4", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22"]
            cl, cr = st.columns(2, gap="large")
            with cl:
                st.markdown(f"**{m_name} - over time**")
                ts = go.Figure()
                for i, z in enumerate(zs):
                    d = zsub[zsub["zone"] == z]
                    ts.add_trace(go.Scatter(x=d["time"], y=d[col], name=z, mode="lines",
                                            line=dict(color=palette[i % 8], width=1.3)))
                ts.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0), hovermode="x unified",
                                 legend=dict(orientation="h", y=1.1))
                st.plotly_chart(ts, key="imb_cmp_t")
            with cr:
                st.markdown(f"**{m_name} - average by {PER} of the day**")
                pr = go.Figure()
                for i, z in enumerate(zs):
                    g = zsub[zsub["zone"] == z].groupby("slot_i" if QH else "hour")[col].mean().reindex(range(NP))
                    pr.add_trace(go.Scatter(x=[hour_label(h) for h in g.index], y=g.values, name=z,
                                            mode="lines+markers", line=dict(color=palette[i % 8])))
                pr.update_xaxes(type="category", tickangle=-60)
                pr.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", y=1.1))
                st.plotly_chart(pr, key="imb_cmp_h")

        rows = []
        for z in zs:
            d = zsub[zsub["zone"] == z]
            v = d["volume_mwh"].dropna()
            pm = product_means(d, "premium")
            m = d[d["premium"].notna()] if d["premium"].notna().any() else d  # like-for-like hours
            rows.append({
                "Zone": z, "Quoted in": cur.get(z, "?"),
                "Avg imbalance price": m["price_sys"].mean(), "Avg spot": m["spot"].mean(),
                "Avg premium": m["premium"].mean(),
                "Premium peak": pm["Peak"].get(z, np.nan), "Premium off-peak": pm["Off-peak"].get(z, np.nan),
                "% of periods above spot": (d["premium"].dropna() > 0).mean() * 100 if d["premium"].notna().any() else np.nan,
                f"Avg volume (MWh per {PER})": v.mean() if len(v) else np.nan,
                "% of periods surplus (down)": (v > 0).mean() * 100 if len(v) else np.nan,
                "% of periods deficit (up)": (v < 0).mean() * 100 if len(v) else np.nan,
                f"{PER.capitalize()}s with data": len(d),
            })
        st.markdown("**Summary over the period** (EUR/MWh; price columns use only hours that have both a spot and an imbalance price)")
        st.dataframe(pd.DataFrame(rows).set_index("Zone"), column_config={
            c: st.column_config.NumberColumn(format="%.1f") for c in
            ["Avg imbalance price", "Avg spot", "Avg premium", "Premium peak", "Premium off-peak",
             "% of periods above spot", f"Avg volume (MWh per {PER})", "% of periods surplus (down)", "% of periods deficit (up)"]})
        st.download_button("Download imbalance data for these zones (CSV)",
                           zsub.drop(columns=["date", "hour"]).to_csv(index=False).encode(),
                           file_name="imbalance_selected.csv", mime="text/csv")


# ---------------------------------------------------------------- regulation tab
REG_HELP = """
**What this tab shows.** ENTSO-E's public API does not publish the *activated* balancing energy volumes (the MWh
the TSOs actually called up). It does publish two things that describe regulation, and both are shown here:

- **Balancing state (MW)** - the TSO's minute-by-minute balancing need ("Current balancing state"). **Up** (green,
  bull) = the system is short and needs *upregulation*; **down** (red, bear) = the system is long and needs
  *downregulation*. **Net** = up minus down. Shown as the mean MW per period. Not every TSO publishes it.
- **Available regulation bids (MW)** - the volume of balancing energy bids on offer for upward and downward
  regulation, for **aFRR** (automatic frequency restoration, the fast continuous regulation) and **mFRR**
  (manual frequency restoration, called up in larger steps). This is the room the TSO has to regulate, not what it
  used.

Hourly view = mean of the quarters. Germany is the sum of its four control areas. Zones missing here do not publish
the data; the coverage table at the bottom lists what each zone returned.
"""
REG_UP_COLORS = {"afrr": "#1b7a34", "mfrr": "#69db7c"}
REG_DOWN_COLORS = {"afrr": "#a61e1e", "mfrr": "#ff8787"}
REG_METRICS = {
    "Net balancing state (up - down)": "net_mw",
    "Upregulation need": "state_up_mw",
    "Downregulation need": "state_down_mw",
}
REG_BIDS = {"aFRR": "afrr", "mFRR": "mfrr"}


@st.cache_data(show_spinner=False)
def load_regulation(mtime: float, gran_key: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / "regulation_15min.csv", parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    if gran_key == "Hourly":
        value_cols = [c for c in df.columns if c not in ("zone", "time")]
        df = (df.set_index("time").groupby("zone")[value_cols].resample("h").mean()
                .dropna(how="all").reset_index())
    df["net_mw"] = np.where(df["state_up_mw"].isna() & df["state_down_mw"].isna(), np.nan,
                            df["state_up_mw"].fillna(0) - df["state_down_mw"].fillna(0))
    df["time"] = df["time"].dt.tz_convert(MARKET_TZ)
    df["date"] = df["time"].dt.date
    df["hour"] = df["time"].dt.hour
    df["slot_i"] = df["time"].dt.hour * 4 + df["time"].dt.minute // 15
    return df.sort_values(["zone", "time"])


with tab_reg:
    reg_path = DATA / "regulation_15min.csv"
    if not reg_path.exists():
        st.info("No regulation data yet. It is fetched by the regulation workflow (python -m pipeline regulation) - "
                "run the 'Update regulation' workflow once to create it.")
    else:
        reg = load_regulation(reg_path.stat().st_mtime, gran)
        ravail = [z for z in ZONE_CODES if z in set(reg["zone"])]
        r_status = {}
        if (DATA / "regulation_status.json").exists():
            r_status = json.loads((DATA / "regulation_status.json").read_text())

        c1, c2 = st.columns([3, 1])
        rdefault = next((z for z in ["NL", "BE", "FR", "DE_LU", "AT"] if z in ravail), ravail[0])
        with c1:
            rzone = st.selectbox("Zone", ravail, index=ravail.index(rdefault), format_func=label, key="reg_zone")
        with c2:
            rdays = st.select_slider("Past days", [7, 14, 21, 30], value=30, key="reg_days")
        with st.expander("How to read this"):
            st.markdown(REG_HELP)

        rlast = reg["date"].max()
        rwin = reg[reg["date"] > rlast - pd.Timedelta(days=rdays)]
        rd = rwin[rwin["zone"] == rzone]
        has_state = rd["net_mw"].notna().any()
        bid_cols = [c for c in rd.columns if c.endswith(("_bid_up_mw", "_bid_down_mw")) and rd[c].notna().any()]
        st.caption(f"{label(rzone)} · {rwin['date'].min():%d %b} - {rlast:%d %b %Y} (market time) · "
                   f"{len(ravail)} zones publish regulation data · "
                   f"{'balancing state' if has_state else 'no balancing state'}, "
                   f"bids: {', '.join(sorted({c.split('_')[0] for c in bid_cols})) or 'none'}")

        # ---- balancing state
        st.subheader(f"{rzone}: balancing state")
        if not has_state:
            st.info(f"{rzone} does not publish a balancing state (MW). See the bids below, if any.")
        else:
            rm_name = st.radio("Show", list(REG_METRICS), horizontal=True, key="reg_metric")
            rcol = REG_METRICS[rm_name]
            hp = rd.pivot_table(index="slot_i" if QH else "hour", columns="date", values=rcol,
                                aggfunc="mean").reindex(range(NP))
            newest_first = sorted(hp.columns, reverse=True)
            hp = hp[newest_first]
            lo, hi = np.nanpercentile(rd[rcol].dropna(), [2, 98])
            if rcol == "net_mw":
                m = float(max(abs(lo), abs(hi))) or 1.0
                scale = dict(colorscale="RdYlGn", zmin=-m, zmax=m, zmid=0)  # green = up (bull), red = down (bear)
            else:
                scale = dict(colorscale="Greens" if rcol == "state_up_mw" else "Reds", zmin=0,
                             zmax=float(max(hi, 1e-6)))
            hm = go.Figure(go.Heatmap(
                z=hp.to_numpy(), x=[d.strftime("%a %d %b") for d in newest_first],
                y=[hour_label(h) for h in range(NP)], xgap=1, ygap=1, texttemplate="%{z:.0f}",
                textfont=dict(size=8 if QH else 9), colorbar=dict(title="MW", thickness=14),
                hovertemplate="%{x}, %{y}: %{z:.1f} MW<extra></extra>", **scale,
            ))
            hm.update_yaxes(autorange="reversed", type="category")
            hm.update_xaxes(type="category", side="top", tickangle=-90)
            hm.update_layout(height=190 + (20 if QH else 25) * NP, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(hm, key="reg_hm")

            def state_bars(x, frame):
                f = go.Figure()
                f.add_trace(go.Bar(x=x, y=frame["state_up_mw"], name="Up (upregulation need)", marker_color="#2f9e44"))
                f.add_trace(go.Bar(x=x, y=-frame["state_down_mw"], name="Down (downregulation need)",
                                   marker_color="#e03131"))
                f.add_trace(go.Scatter(x=x, y=frame["net_mw"], name="Net", mode="lines",
                                       line=dict(color="#1c1c1c", width=1.4)))
                f.update_layout(barmode="relative", height=400, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="MW",
                                hovermode="x unified", legend=dict(orientation="h", y=1.12), bargap=0)
                return f

            left, right = st.columns(2, gap="large")
            with left:
                st.markdown("**Over time** (up above zero, down below)")
                st.plotly_chart(state_bars(rd["time"], rd), key="reg_t")
            with right:
                st.markdown(f"**Average by {PER} of the day**")
                g = rd.groupby("slot_i" if QH else "hour")[["state_up_mw", "state_down_mw", "net_mw"]].mean()
                g = g.reindex(range(NP))
                fh = state_bars([hour_label(h) for h in g.index], g)
                fh.update_xaxes(type="category", tickangle=-60)
                st.plotly_chart(fh, key="reg_h")

            rz = rd.assign(zone=rzone)
            ptab = pd.DataFrame({"Up (MW)": product_means(rz, "state_up_mw").loc[rzone],
                                 "Down (MW)": product_means(rz, "state_down_mw").loc[rzone],
                                 "Net (MW)": product_means(rz, "net_mw").loc[rzone]}).T
            st.markdown(f"**{rzone} by product** (average MW)")
            st.dataframe(ptab, column_config={c: st.column_config.NumberColumn(format="%.1f") for c in PRODUCTS})
            st.caption(PRODUCT_HELP)

        # ---- available bids
        st.subheader(f"{rzone}: available regulation bids")
        if not bid_cols:
            st.info(f"{rzone} has no published aggregated balancing energy bids in this period.")
        else:
            fb = go.Figure()
            for name, key in REG_BIDS.items():
                if f"{key}_bid_up_mw" in bid_cols:
                    fb.add_trace(go.Scatter(x=rd["time"], y=rd[f"{key}_bid_up_mw"], name=f"{name} up",
                                            mode="lines", line=dict(color=REG_UP_COLORS[key], width=1.5)))
                if f"{key}_bid_down_mw" in bid_cols:
                    fb.add_trace(go.Scatter(x=rd["time"], y=-rd[f"{key}_bid_down_mw"], name=f"{name} down",
                                            mode="lines", line=dict(color=REG_DOWN_COLORS[key], width=1.5)))
            fb.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="MW offered (down below zero)",
                             hovermode="x unified", legend=dict(orientation="h", y=1.1))
            st.plotly_chart(fb, key="reg_bids")

        # ---- all zones
        st.subheader("All zones")
        zrows = []
        for z, gz in rwin.groupby("zone"):
            up, dn = gz["state_up_mw"].mean(), gz["state_down_mw"].mean()
            both = gz[["state_up_mw", "state_down_mw"]].dropna()
            zrows.append({
                "Zone": z, "Name": ZONE_NAME[z], "Region": ZONE_REGION[z],
                "Up need (MW)": up, "Down need (MW)": dn,
                "Net (MW)": gz["net_mw"].mean(),
                "% of periods net up": (gz["net_mw"].dropna() > 0).mean() * 100 if gz["net_mw"].notna().any() else np.nan,
                "aFRR bids up (MW)": gz["afrr_bid_up_mw"].mean(), "aFRR bids down (MW)": gz["afrr_bid_down_mw"].mean(),
                "mFRR bids up (MW)": gz["mfrr_bid_up_mw"].mean(), "mFRR bids down (MW)": gz["mfrr_bid_down_mw"].mean(),
            })
        zt = pd.DataFrame(zrows).set_index("Zone")
        zs = zt.dropna(subset=["Net (MW)"]).sort_values("Net (MW)", ascending=False)
        if len(zs):
            bar = go.Figure(go.Bar(x=zs.index, y=zs["Net (MW)"],
                                   marker_color=np.where(zs["Net (MW)"] >= 0, "#2f9e44", "#e03131")))
            bar.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="Net balancing state (MW)")
            st.plotly_chart(bar, key="reg_all")
        st.dataframe(zt, column_config={c: st.column_config.NumberColumn(format="%.0f") for c in zt.columns
                                        if c not in ("Name", "Region")})
        st.download_button("Download regulation data for this zone (CSV)",
                           rd.drop(columns=["date", "hour", "slot_i"]).to_csv(index=False).encode(),
                           file_name=f"regulation_{rzone}.csv", mime="text/csv")

        if r_status:
            with st.expander("Data coverage per zone"):
                cov = pd.DataFrame([{"Zone": z, "Status": v.get("status"), "Series": ", ".join(v.get("series", [])),
                                     "Checked": v.get("checked"), "Error": v.get("error", "")}
                                    for z, v in r_status.items()]).set_index("Zone")
                st.dataframe(cov)
