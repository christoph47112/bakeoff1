import streamlit as st
import pandas as pd
import numpy as np
from io import BytesIO

st.set_page_config(page_title="Bake-Off Vorschläge", layout="wide")

st.title("🥐 Bake-Off: Nachfrage → Back- & Bestellvorschläge (MVP)")

st.markdown("""
Dieses MVP:
- lädt deine Excel-Dateien (Frequenz/Verkäufe, Bestandsverlauf, Abschriften)
- baut stündliche Profile (Wochentag × Stunde)
- erkennt potenzielle OOS-Stunden (Bestand = 0) und imputiert Nachfrage grob
- erzeugt Vorschläge pro Artikel für ein gewähltes Zeitfenster
""")

@st.cache_data
def read_excel(file) -> pd.DataFrame:
    return pd.read_excel(file)

def to_datetime_safe(s):
    return pd.to_datetime(s, errors="coerce", dayfirst=True)

def round_to_batch(x, batch):
    if batch <= 1:
        return int(np.ceil(x))
    return int(np.ceil(x / batch) * batch)

def download_df(df: pd.DataFrame, filename: str):
    out = BytesIO()
    df.to_csv(out, index=False, encoding="utf-8")
    st.download_button(
        label=f"⬇️ {filename} herunterladen",
        data=out.getvalue(),
        file_name=filename,
        mime="text/csv"
    )

st.sidebar.header("1) Dateien hochladen")

freq_file = st.sidebar.file_uploader("FREQUENZ / Verkäufe (Excel)", type=["xlsx"])
stock_file = st.sidebar.file_uploader("BESTANDSVERLAUF (Excel)", type=["xlsx"])
waste_file = st.sidebar.file_uploader("ABSCHRIFTEN (Excel)", type=["xlsx"])

if not (freq_file and stock_file and waste_file):
    st.info("⬅️ Bitte lade links alle 3 Dateien hoch.")
    st.stop()

freq = read_excel(freq_file)
stock = read_excel(stock_file)
waste = read_excel(waste_file)

st.subheader("2) Vorschau")
c1, c2, c3 = st.columns(3)
with c1:
    st.write("Frequenz/Verkäufe")
    st.dataframe(freq.head(10), use_container_width=True)
with c2:
    st.write("Bestandsverlauf")
    st.dataframe(stock.head(10), use_container_width=True)
with c3:
    st.write("Abschriften")
    st.dataframe(waste.head(10), use_container_width=True)

st.divider()
st.sidebar.header("2) Spalten-Mapping")

# ---------- Frequenz Mapping ----------
st.sidebar.subheader("Frequenz/Verkäufe")
freq_cols = list(freq.columns)

freq_sku = st.sidebar.selectbox("SKU/Artikel-ID Spalte", freq_cols, index=0)
freq_date = st.sidebar.selectbox("Datums-Spalte", freq_cols, index=min(1, len(freq_cols)-1))
freq_hour = st.sidebar.selectbox("Stunden-Spalte (oder Zeitfenster)", freq_cols, index=min(2, len(freq_cols)-1))
freq_qty = st.sidebar.selectbox("Mengen/Absatz Spalte", freq_cols, index=min(3, len(freq_cols)-1))

# ---------- Bestand Mapping ----------
st.sidebar.subheader("Bestand")
stock_cols = list(stock.columns)

stock_sku = st.sidebar.selectbox("SKU/Artikel-ID Spalte (Bestand)", stock_cols, index=0)
stock_date = st.sidebar.selectbox("Datums/Datetime Spalte (Bestand)", stock_cols, index=min(1, len(stock_cols)-1))
stock_qty = st.sidebar.selectbox("Bestands-Spalte", stock_cols, index=min(2, len(stock_cols)-1))

# ---------- Abschriften Mapping ----------
st.sidebar.subheader("Abschriften")
waste_cols = list(waste.columns)

waste_sku = st.sidebar.selectbox("SKU/Artikel-ID Spalte (Abschriften)", waste_cols, index=0)
waste_qty = st.sidebar.selectbox("Abschriften-Menge Spalte", waste_cols, index=min(1, len(waste_cols)-1))

st.sidebar.header("3) Parameter")
batch_size = st.sidebar.number_input("Batch-/Blechgröße (Rundung)", min_value=1, max_value=48, value=6, step=1)
weeks_lookback = st.sidebar.slider("Lookback Wochen für Profile", 2, 26, 8)
service_level = st.sidebar.slider("Service-Level (mehr = weniger OOS, mehr Waste)", 0.50, 0.95, 0.80, 0.05)

st.sidebar.header("4) Vorschlags-Fenster")
target_weekday = st.sidebar.selectbox("Wochentag", ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"])
target_hour = st.sidebar.slider("Stunde (0-23)", 0, 23, 7)

st.divider()
st.subheader("3) Daten normalisieren")

# ---- Normalize freq ----
f = freq[[freq_sku, freq_date, freq_hour, freq_qty]].copy()
f.columns = ["sku", "date_raw", "hour_raw", "sales_qty"]

f["date"] = to_datetime_safe(f["date_raw"]).dt.date

# hour parsing: allow "06:00-07:00" or "06:00" or 6
def parse_hour(x):
    if pd.isna(x):
        return np.nan
    if isinstance(x, (int, float)) and not np.isnan(x):
        h = int(x)
        if 0 <= h <= 23:
            return h
    s = str(x).strip()
    # handle "06:00-07:00"
    if "-" in s:
        s = s.split("-")[0].strip()
    # handle "06:00"
    if ":" in s:
        try:
            h = int(s.split(":")[0])
            if 0 <= h <= 23:
                return h
        except:
            return np.nan
    # fallback
    try:
        h = int(s)
        if 0 <= h <= 23:
            return h
    except:
        return np.nan
    return np.nan

f["hour"] = f["hour_raw"].apply(parse_hour).astype("Int64")
f["sales_qty"] = pd.to_numeric(f["sales_qty"], errors="coerce").fillna(0.0)

# ---- Normalize stock ----
s = stock[[stock_sku, stock_date, stock_qty]].copy()
s.columns = ["sku", "dt_raw", "stock_qty"]
s["dt"] = to_datetime_safe(s["dt_raw"])
# If only date exists, dt time becomes 00:00. That's fine for MVP.
s["date"] = s["dt"].dt.date
s["stock_qty"] = pd.to_numeric(s["stock_qty"], errors="coerce")

# keep last snapshot of day as stock_end (simple)
s_day = (s.dropna(subset=["date"])
           .sort_values(["sku","dt"])
           .groupby(["sku","date"], as_index=False)
           .tail(1)[["sku","date","stock_qty"]]
        ).rename(columns={"stock_qty": "stock_end"})

# ---- Normalize waste ----
w = waste[[waste_sku, waste_qty]].copy()
w.columns = ["sku", "waste_qty"]
w["waste_qty"] = pd.to_numeric(w["waste_qty"], errors="coerce").fillna(0.0)

st.write("✅ Normalisierung abgeschlossen.")
c1, c2, c3 = st.columns(3)
with c1:
    st.write("Frequenz (normalisiert)")
    st.dataframe(f.head(8), use_container_width=True)
with c2:
    st.write("Bestand daily stock_end")
    st.dataframe(s_day.head(8), use_container_width=True)
with c3:
    st.write("Abschriften (SKU total)")
    st.dataframe(w.head(8), use_container_width=True)

st.divider()
st.subheader("4) Merge + Profile")

# Merge: hourly sales + daily stock_end
df = f.merge(s_day, on=["sku","date"], how="left")
df["stock_end"] = df["stock_end"].fillna(-1)  # -1 = unknown (MVP)
df["is_oos"] = (df["stock_end"] == 0)

# demand_hat: if likely OOS hour → impute from history median of same weekday/hour
df["date_dt"] = pd.to_datetime(df["date"])
df["weekday"] = df["date_dt"].dt.day_name()

# restrict lookback
max_date = df["date_dt"].max()
min_date = max_date - pd.Timedelta(days=int(weeks_lookback*7))
hist = df[df["date_dt"].between(min_date, max_date)].copy()

# median profile
profile_med = (hist
    .groupby(["sku","weekday","hour"], as_index=False)["sales_qty"]
    .median()
    .rename(columns={"sales_qty":"median_qty"})
)

# std profile for buffer
profile_std = (hist
    .groupby(["sku","weekday","hour"], as_index=False)["sales_qty"]
    .std(ddof=0)
    .rename(columns={"sales_qty":"std_qty"})
)
profile = profile_med.merge(profile_std, on=["sku","weekday","hour"], how="left")
profile["std_qty"] = profile["std_qty"].fillna(0.0)

# apply demand_hat
df = df.merge(profile_med, on=["sku","weekday","hour"], how="left")
df["median_qty"] = df["median_qty"].fillna(df["sales_qty"].median())
df["demand_hat"] = np.where(df["is_oos"], np.maximum(df["sales_qty"], df["median_qty"]), df["sales_qty"])

# waste rate
sales_total = df.groupby("sku", as_index=False)["sales_qty"].sum().rename(columns={"sales_qty":"sales_total"})
kpi = sales_total.merge(w, on="sku", how="left")
kpi["waste_qty"] = kpi["waste_qty"].fillna(0.0)
kpi["waste_rate"] = kpi["waste_qty"] / (kpi["sales_total"] + kpi["waste_qty"] + 1e-9)

st.write("📌 Profil-Beispiel (Median/Std je SKU×Wochentag×Stunde):")
st.dataframe(profile.head(20), use_container_width=True)

st.divider()
st.subheader("5) Vorschläge generieren")

# Target profile slice
target_profile = profile[(profile["weekday"] == target_weekday) & (profile["hour"] == target_hour)].copy()
target_profile = target_profile.merge(kpi[["sku","waste_rate"]], on="sku", how="left")
target_profile["waste_rate"] = target_profile["waste_rate"].fillna(0.0)

# Simple buffer: service_level * std, damped by waste_rate
# waste penalty: if waste_rate high -> reduce buffer
target_profile["buffer"] = (service_level * target_profile["std_qty"]) * (1.0 - np.clip(target_profile["waste_rate"], 0, 0.6))

# Need qty for next hour window (MVP)
target_profile["forecast_qty"] = target_profile["median_qty"]
target_profile["suggest_bake"] = (target_profile["forecast_qty"] + target_profile["buffer"]).clip(lower=0)

# Round to batch
target_profile["suggest_bake_rounded"] = target_profile["suggest_bake"].apply(lambda x: round_to_batch(x, batch_size))

# Attach latest known stock_end (most recent day)
latest_day = df["date_dt"].max().date()
latest_stock = s_day[s_day["date"] == latest_day][["sku","stock_end"]]
target_profile = target_profile.merge(latest_stock, on="sku", how="left")

# If stock known: subtract current stock (rough)
target_profile["suggest_bake_net"] = target_profile["suggest_bake_rounded"]
mask_known = target_profile["stock_end"].notna() & (target_profile["stock_end"] >= 0)
target_profile.loc[mask_known, "suggest_bake_net"] = (
    target_profile.loc[mask_known, "suggest_bake_rounded"] - target_profile.loc[mask_known, "stock_end"]
).clip(lower=0)

target_profile["suggest_bake_net"] = target_profile["suggest_bake_net"].apply(lambda x: round_to_batch(x, batch_size))

# Sort by biggest recommendation
out = target_profile.sort_values("suggest_bake_net", ascending=False).reset_index(drop=True)

st.write(f"🎯 Vorschläge für **{target_weekday}** um **{target_hour}:00** (Batch={batch_size}):")
st.dataframe(
    out[["sku","forecast_qty","std_qty","buffer","waste_rate","stock_end","suggest_bake_net"]],
    use_container_width=True
)

download_df(out, "bakeoff_vorschlaege.csv")

st.divider()
st.subheader("6) Hinweise (MVP-Limitierungen)")
st.markdown("""
- Wenn Bestände nur 1× pro Tag vorliegen, ist OOS-Erkennung grob.
- Abschriften ohne Datum/Zeit wirken nur als KPI (waste_rate), nicht tagesgenau.
- Für echte Bestellvorschläge mit Lieferintervallen brauchst du Lead-Time + Kartongrößen.
""")
