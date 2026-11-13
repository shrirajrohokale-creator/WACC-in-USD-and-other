"""
International Cost of Capital App
=================================
Compares a project's cost of equity, cost of debt and WACC in USD and in the
currency of a selected country.

Data sources (fetched live, with a refresh button):
  * Unlevered industry betas ........ Damodaran (NYU Stern) betas.xls
  * BBB (Baa2) USD default spread ... Damodaran ratings table (large non-financial firms)
  * Corporate tax rates ............. OECD Corporate Tax Statistics (statutory rates)
  * 10-year government bond yields .. Trading Economics /bonds

Run with:  streamlit run app.py
"""

from __future__ import annotations

import io
import re
import unicodedata
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
BETAS_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
RATINGS_URL = "https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html"
OECD_URL = (
    "https://www.oecd.org/en/publications/corporate-tax-statistics-2024_9c27d6e8-en/"
    "full-report/statutory-corporate-income-tax-rates_440f9286.html"
)
TE_URL = "https://tradingeconomics.com/bonds"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 30

DEFAULT_INDUSTRY = "Software (System & Application)"
DEFAULT_COUNTRY = "United States"
DEFAULT_ERP = 6.5   # %
DEFAULT_DEBT = 30   # %
OTHER_COUNTRY = "Other (enter manually)"

# Used ONLY if the live OECD fetch fails. Approximate 2024 combined statutory
# corporate income tax rates (%). Clearly labelled in the UI; verify or override.
OECD_FALLBACK = {
    "Australia": 30.0, "Austria": 23.0, "Belgium": 25.0, "Canada": 26.2, "Chile": 27.0,
    "Colombia": 35.0, "Costa Rica": 30.0, "Czech Republic": 21.0, "Denmark": 22.0,
    "Estonia": 20.0, "Finland": 20.0, "France": 25.83, "Germany": 29.9, "Greece": 22.0,
    "Hungary": 9.0, "Iceland": 21.0, "Ireland": 12.5, "Israel": 23.0, "Italy": 24.0,
    "Japan": 29.74, "South Korea": 24.0, "Latvia": 20.0, "Lithuania": 15.0,
    "Luxembourg": 23.87, "Mexico": 30.0, "Netherlands": 25.8, "New Zealand": 28.0,
    "Norway": 22.0, "Poland": 19.0, "Portugal": 31.5, "Slovakia": 21.0, "Slovenia": 22.0,
    "Spain": 25.0, "Sweden": 20.6, "Switzerland": 19.6, "Turkey": 25.0,
    "United Kingdom": 25.0, "United States": 25.77, "Argentina": 35.0, "Brazil": 34.0,
    "China": 25.0, "India": 25.17, "Indonesia": 22.0, "Russia": 20.0,
    "Saudi Arabia": 20.0, "South Africa": 27.0, "Singapore": 17.0, "Hong Kong": 16.5,
    "Malaysia": 24.0, "Thailand": 20.0, "Vietnam": 20.0, "Philippines": 25.0,
    "Egypt": 22.5, "Nigeria": 30.0, "Peru": 29.5, "Romania": 16.0, "Bulgaria": 10.0,
    "Croatia": 18.0, "Cyprus": 12.5, "Malta": 35.0,
}
# Used ONLY if the live Trading Economics fetch fails (snapshot of the table on 12-Nov-2026).
TE_FALLBACK = {
    "United States": 5.1090, "United Kingdom": 5.2816, "Japan": 2.7930,
    "Australia": 5.0650, "Germany": 3.3573, "Brazil": 15.3750, "Russia": 16.7300,
    "India": 7.2480,
}

ALIASES = {
    "turkiye": "turkey", "korea": "south korea", "republic of korea": "south korea",
    "slovak republic": "slovakia", "czechia": "czech republic",
    "russian federation": "russia", "peoples republic of china": "china",
    "united states of america": "united states", "hong kong china": "hong kong",
    "korea rep": "south korea", "us": "united states", "uk": "united kingdom",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def norm_country(name: str) -> str:
    """Normalise a country name so OECD and Trading Economics names can be matched."""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\[.*?\]|\(.*?\)", lambda m: m.group(0) if "china" in m.group(0) else "", s)
    s = re.sub(r"[^a-z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return ALIASES.get(s, s)


def to_float(x):
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(x).replace(",", "")))
    except ValueError:
        return None


def http_get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------- #
# Data fetchers (each returns a dict and never raises)
# --------------------------------------------------------------------------- #
def parse_betas(content: bytes) -> tuple[pd.DataFrame, str]:
    """Parse Damodaran's betas.xls -> DataFrame[Industry, Firms, UnleveredBeta], data date."""
    book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    for sheet, raw in book.items():
        # locate the header row containing "Industry Name"
        hdr_idx = None
        for i in range(min(40, len(raw))):
            vals = [str(v).strip().lower() for v in raw.iloc[i].tolist()]
            if "industry name" in vals:
                hdr_idx = i
                break
        if hdr_idx is None:
            continue
        header = [str(v).strip() for v in raw.iloc[hdr_idx].tolist()]
        df = raw.iloc[hdr_idx + 1:].copy()
        df.columns = header
        lower = {c.lower(): c for c in header}
        ub_col = lower.get("unlevered beta")
        if ub_col is None:  # tolerate small naming drift, but never the cash-corrected one
            cands = [c for c in header if c.lower().startswith("unlevered beta")
                     and "cash" not in c.lower()]
            ub_col = cands[0] if cands else None
        if ub_col is None:
            continue
        firms_col = next((c for c in header if "number of firms" in c.lower()), None)
        out = pd.DataFrame({
            "Industry": df[lower["industry name"]].astype(str).str.strip(),
            "Firms": pd.to_numeric(df[firms_col], errors="coerce") if firms_col else pd.NA,
            "Unlevered beta": pd.to_numeric(df[ub_col], errors="coerce"),
        })
        out = out.dropna(subset=["Unlevered beta"])
        out = out[~out["Industry"].str.lower().isin(["total market", "total market (without financials)", "nan"])]
        out = out.reset_index(drop=True)
        # data date: look for "Date of analysis" or a "Month YYYY" string near the top
        date_txt = ""
        top = " | ".join(str(v) for v in raw.iloc[:hdr_idx + 1].values.flatten() if str(v) != "nan")
        m = re.search(r"Date of analysis:?\s*([^|]+)", top, re.I)
        if m:
            date_txt = m.group(1).strip()
        else:
            m = re.search(r"(January|February|March|April|May|June|July|August|September|"
                          r"October|November|December)\s+\d{4}", top)
            date_txt = m.group(0) if m else ""
        if len(out):
            return out, date_txt
    raise ValueError("Could not find 'Industry Name' / 'Unlevered beta' columns in workbook")


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_betas() -> dict:
    try:
        r = http_get(BETAS_URL)
        df, date_txt = parse_betas(r.content)
        lm = r.headers.get("Last-Modified", "")
        return {"ok": True, "df": df, "data_date": date_txt or lm or "not stated",
                "file_modified": lm, "fetched": now_utc(), "url": BETAS_URL, "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "df": None, "data_date": "", "fetched": now_utc(),
                "url": BETAS_URL, "error": f"{type(e).__name__}: {e}"}


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_bbb_spread() -> dict:
    """BBB (Baa2/BBB) spread for large non-financial service firms, in %."""
    try:
        r = http_get(RATINGS_URL)
        html = r.text
        spread = None
        # first table containing Baa2/BBB is the large non-financial firm table
        for tbl in pd.read_html(io.StringIO(html)):
            for _, row in tbl.iterrows():
                cells = [str(c) for c in row.tolist()]
                if any(re.fullmatch(r"\s*Baa2\s*/\s*BBB\s*", c) for c in cells):
                    pct = [c for c in cells if c.strip().endswith("%")]
                    if pct:
                        spread = to_float(pct[-1])
                        break
            if spread is not None:
                break
        if spread is None:
            raise ValueError("Baa2/BBB row not found")
        text = BeautifulSoup(html, "lxml").get_text(" ")
        m = re.search(r"(?:as of|updated|data (?:used )?(?:as of|on))\s*:?\s*"
                      r"((?:January|February|March|April|May|June|July|August|September|"
                      r"October|November|December)\s+\d{1,2}?,?\s*\d{4})", text, re.I)
        if not m:
            m = re.search(r"(January|February|March|April|May|June|July|August|September|"
                          r"October|November|December)\s+\d{4}", text)
        date_txt = (m.group(1) if m and m.lastindex else m.group(0)) if m else "not stated"
        return {"ok": True, "spread": spread, "data_date": date_txt, "fetched": now_utc(),
                "url": RATINGS_URL, "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "spread": None, "data_date": "", "fetched": now_utc(),
                "url": RATINGS_URL, "error": f"{type(e).__name__}: {e}"}


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_oecd_tax() -> dict:
    """Latest combined statutory CIT rate (%) per country from the OECD page."""
    try:
        r = http_get(OECD_URL)
        best = None
        for tbl in pd.read_html(io.StringIO(r.text)):
            tbl = tbl.copy()
            tbl.columns = [" ".join(map(str, c)) if isinstance(c, tuple) else str(c)
                           for c in tbl.columns]
            first = tbl.columns[0]
            names = tbl[first].astype(str)
            if not names.str.contains("United States", case=False).any():
                continue
            year_cols = {}
            for c in tbl.columns[1:]:
                m = re.search(r"(19|20)\d{2}", c)
                if m:
                    year_cols[c] = int(m.group(0))
            if not year_cols:
                continue
            latest = max(year_cols, key=year_cols.get)
            rates = {}
            for nm, val in zip(names, tbl[latest]):
                v = to_float(val)
                if v is not None and 0 <= v <= 60:
                    rates[re.sub(r"\s*\[.*?\]|\s*\d+$", "", nm).strip()] = v
            if len(rates) > 20:
                best = (rates, year_cols[latest])
                break
        if best is None:
            raise ValueError("statutory rate table not found in page HTML")
        rates, year = best
        return {"ok": True, "rates": rates, "year": year, "data_date": f"{year} statutory rates",
                "fetched": now_utc(), "url": OECD_URL, "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "rates": dict(OECD_FALLBACK), "year": 2024,
                "data_date": "2024 (bundled approximate snapshot - live fetch failed)",
                "fetched": now_utc(), "url": OECD_URL, "error": f"{type(e).__name__}: {e}"}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_yields() -> dict:
    """10-year government bond yields (%) from Trading Economics."""
    try:
        r = http_get(TE_URL)
        soup = BeautifulSoup(r.text, "lxml")
        yields, dates = {}, {}
        for tr in soup.select("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            texts = [td.get_text(" ", strip=True) for td in tds]
            name_i = next((i for i, t in enumerate(texts)
                           if t and to_float(t) is None and re.search(r"[A-Za-z]", t)), None)
            if name_i is None or name_i + 1 >= len(texts):
                continue
            y = to_float(texts[name_i + 1]) if re.match(r"^-?\d", texts[name_i + 1]) else None
            if y is None:
                continue
            name = texts[name_i].strip()
            if name in yields:
                continue
            yields[name] = y
            d = texts[-1]
            dates[name] = d if re.match(r"^[A-Z][a-z]{2}/\d{1,2}$|^\d{4}-\d{2}-\d{2}$", d) else ""
        if len(yields) < 5:
            raise ValueError(f"only {len(yields)} yields parsed (page may be JS-rendered or blocked)")
        return {"ok": True, "yields": yields, "dates": dates, "fetched": now_utc(),
                "data_date": max(set(dates.values()), key=list(dates.values()).count) or "not stated",
                "url": TE_URL, "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "yields": dict(TE_FALLBACK), "dates": {k: "Nov/12" for k in TE_FALLBACK},
                "fetched": now_utc(), "data_date": "Nov/12 2026 (bundled snapshot - live fetch failed)",
                "url": TE_URL, "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# Finance calculations
# --------------------------------------------------------------------------- #
def relever_beta(beta_u: float, tax: float, debt_share: float) -> float:
    """Hamada: beta_L = beta_U * (1 + (1 - t) * D/E)."""
    de = debt_share / (1 - debt_share) if debt_share < 1 else float("inf")
    return beta_u * (1 + (1 - tax) * de)


def fx_convert(rate_usd: float, rf_us: float, rf_f: float) -> float:
    """Convert a USD nominal rate to the foreign currency with the interest-rate
    (inflation-differential) parity link: (1+r_F) = (1+r_USD) * (1+rf_F)/(1+rf_US)."""
    return (1 + rate_usd) * (1 + rf_f) / (1 + rf_us) - 1


def cost_of_capital(beta_u, tax, debt_share, erp, rf_us, rf_f, spread_usd, spread_f=None):
    """All inputs as decimals. spread_f=None -> auto-calculated via parity.
    Returns dict with USD and foreign-currency results."""
    beta_l = relever_beta(beta_u, tax, debt_share)
    e_share = 1 - debt_share

    re_usd = rf_us + beta_l * erp
    rd_usd = rf_us + spread_usd
    wacc_usd = e_share * re_usd + debt_share * rd_usd * (1 - tax)

    re_f = fx_convert(re_usd, rf_us, rf_f)
    if spread_f is None:
        rd_f = fx_convert(rd_usd, rf_us, rf_f)
        spread_f_used = rd_f - rf_f
    else:
        rd_f = rf_f + spread_f
        spread_f_used = spread_f
    wacc_f = e_share * re_f + debt_share * rd_f * (1 - tax)

    return {
        "beta_u": beta_u, "beta_l": beta_l, "tax": tax, "debt_share": debt_share,
        "equity_share": e_share, "d_e": debt_share / e_share if e_share else float("inf"),
        "erp": erp, "rf_us": rf_us, "rf_f": rf_f,
        "spread_usd": spread_usd, "spread_f": spread_f_used,
        "re_usd": re_usd, "rd_usd": rd_usd, "rd_usd_at": rd_usd * (1 - tax), "wacc_usd": wacc_usd,
        "re_f": re_f, "rd_f": rd_f, "rd_f_at": rd_f * (1 - tax), "wacc_f": wacc_f,
    }


def pct(x, nd=2):
    return f"{x * 100:.{nd}f}%"


# --------------------------------------------------------------------------- #
# Streamlit UI
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="International Cost of Capital", page_icon="🌍", layout="wide")
st.title("🌍 International Cost of Capital: USD vs. Foreign Currency")
st.caption("Cost of equity, cost of debt and WACC for the same project expressed in USD and in "
           "the currency of a selected country.")

# ---- Sidebar: refresh --------------------------------------------------------
with st.sidebar:
    st.header("Data")
    if st.button("🔄 Refresh data from sources", width="stretch"):
        st.cache_data.clear()
        st.rerun()

with st.spinner("Fetching latest data..."):
    betas = fetch_betas()
    bbb = fetch_bbb_spread()
    oecd = fetch_oecd_tax()
    te = fetch_yields()

# Manual upload fallback if the beta file could not be fetched
beta_df = betas["df"] if betas["ok"] else None
beta_note = ""
if beta_df is None:
    with st.sidebar:
        st.error("Could not download Damodaran's betas.xls automatically. Upload it manually:")
        up = st.file_uploader("betas.xls", type=["xls", "xlsx"])
    if up is not None:
        try:
            beta_df, d = parse_betas(up.getvalue())
            beta_note = f"manually uploaded file ({d or 'date not stated'})"
        except Exception as e:  # noqa: BLE001
            st.sidebar.error(f"Could not parse the uploaded file: {e}")
if beta_df is None:
    st.error(f"Industry beta data is unavailable. Error: {betas['error']}. "
             "Use the refresh button or upload the file in the sidebar.")
    st.stop()

# ---- Sidebar: inputs ---------------------------------------------------------
industries = beta_df["Industry"].tolist()
te_countries = sorted(te["yields"].keys())
country_options = te_countries + [OTHER_COUNTRY]

with st.sidebar:
    st.header("Project inputs")
    industry = st.selectbox(
        "Industry", industries,
        index=industries.index(DEFAULT_INDUSTRY) if DEFAULT_INDUSTRY in industries else 0)
    country = st.selectbox(
        "Target country", country_options,
        index=country_options.index(DEFAULT_COUNTRY) if DEFAULT_COUNTRY in country_options else 0)
    erp_in = st.number_input("Equity risk premium (%)", 0.0, 30.0, DEFAULT_ERP, 0.1, format="%.2f")
    debt_in = st.slider("Target debt share, D/(D+E) (%)", 0, 95, DEFAULT_DEBT, 1)
    tax_choice = st.radio("Project tax rate", ["United States", "Selected country"],
                          horizontal=True, index=0,
                          help="The same tax rate is used for relevering beta and the debt tax shield "
                               "in both currencies.")

# ---- fetched values ----------------------------------------------------------
oecd_by_norm = {norm_country(k): v for k, v in oecd["rates"].items()}
te_by_norm = {norm_country(k): v for k, v in te["yields"].items()}

row = beta_df[beta_df["Industry"] == industry].iloc[0]
beta_fetched = float(row["Unlevered beta"])
bbb_fetched = bbb["spread"] if bbb["ok"] else 1.11
us_tax_fetched = oecd_by_norm.get("united states", 25.77 if not oecd["ok"] else None)
us_yield_fetched = te_by_norm.get("united states")

if country == OTHER_COUNTRY:
    country_label = st.sidebar.text_input("Country name", "Other country")
    f_tax_fetched = None
    f_yield_fetched = None
else:
    country_label = country
    f_tax_fetched = oecd_by_norm.get(norm_country(country))
    f_yield_fetched = te["yields"].get(country)

# ---- Sidebar: manual overrides ------------------------------------------------
with st.sidebar:
    st.header("Manual overrides")
    st.caption("Switch a toggle on to replace the fetched value with your own.")

    ov_beta = st.toggle("Override unlevered beta", False)
    beta_u = st.number_input("Unlevered beta", 0.0, 5.0, round(beta_fetched, 4), 0.01,
                             format="%.4f", disabled=not ov_beta, key=f"beta_{industry}") \
        if ov_beta else beta_fetched

    ov_tax_us = st.toggle("Override U.S. tax rate", False)
    tax_us_pct = st.number_input("U.S. tax rate (%)", 0.0, 60.0,
                                 float(us_tax_fetched) if us_tax_fetched is not None else 25.0, 0.1,
                                 key="tax_us") if ov_tax_us else us_tax_fetched
    if tax_us_pct is None:
        st.warning("U.S. tax rate not found; using 25%. Use the override to change it.")
        tax_us_pct = 25.0

    ov_tax_f = st.toggle("Override selected-country tax rate", f_tax_fetched is None)
    tax_f_pct = st.number_input("Selected-country tax rate (%)", 0.0, 60.0,
                                float(f_tax_fetched) if f_tax_fetched is not None else 25.0, 0.1,
                                key=f"tax_f_{country}") if ov_tax_f else f_tax_fetched
    if f_tax_fetched is None and not ov_tax_f:
        tax_f_pct = 25.0

    ov_spread = st.toggle("Override USD BBB spread", False)
    spread_usd_pct = st.number_input("USD BBB spread (%)", 0.0, 20.0, float(bbb_fetched), 0.01,
                                     format="%.2f", key="spread_usd") if ov_spread else bbb_fetched

    ov_y_us = st.toggle("Override U.S. 10-yr yield", us_yield_fetched is None)
    rf_us_pct = st.number_input("U.S. 10-yr yield (%)", -2.0, 50.0,
                                float(us_yield_fetched) if us_yield_fetched is not None else 4.0, 0.01,
                                format="%.3f", key="y_us") if ov_y_us else us_yield_fetched

    ov_y_f = st.toggle("Override selected-country 10-yr yield", f_yield_fetched is None)
    rf_f_pct = st.number_input("Selected-country 10-yr yield (%)", -2.0, 100.0,
                               float(f_yield_fetched) if f_yield_fetched is not None else 5.0, 0.01,
                               format="%.3f", key=f"y_f_{country}") if ov_y_f else f_yield_fetched

    ov_spread_f = st.toggle("Override foreign BBB spread", False,
                            help="Off = auto-calculated from the USD spread and the yield differential.")

# Foreign spread input needs the auto value, so compute a preview first
_auto_spread_f = bbb_fetched if not ov_spread else spread_usd_pct
_auto_spread_f = _auto_spread_f / 100 * (1 + rf_f_pct / 100) / (1 + rf_us_pct / 100) * 100 \
    if rf_us_pct is not None and rf_f_pct is not None else None
with st.sidebar:
    if ov_spread_f:
        spread_f_pct = st.number_input("Foreign BBB spread (%)", 0.0, 50.0,
                                       round(float(_auto_spread_f or 1.0), 2), 0.01,
                                       format="%.2f", key=f"spread_f_{country}")
    else:
        spread_f_pct = None

# ---- validation ---------------------------------------------------------------
if rf_us_pct is None or rf_f_pct is None:
    st.error("A 10-year yield is missing. Turn on the yield override and enter it manually.")
    st.stop()
if country == DEFAULT_COUNTRY:
    pass

tax_pct = tax_us_pct if tax_choice == "United States" else tax_f_pct
tax_name = "U.S." if tax_choice == "United States" else country_label

# ---- compute ------------------------------------------------------------------
res = cost_of_capital(
    beta_u=beta_u, tax=tax_pct / 100, debt_share=debt_in / 100, erp=erp_in / 100,
    rf_us=rf_us_pct / 100, rf_f=rf_f_pct / 100, spread_usd=spread_usd_pct / 100,
    spread_f=None if spread_f_pct is None else spread_f_pct / 100)

# ---- data status ---------------------------------------------------------------
with st.expander("📡 Data sources, dates and status", expanded=False):
    status = pd.DataFrame([
        {"Item": "Unlevered industry betas", "Source": "Damodaran betas.xls",
         "Data date": betas["data_date"] if betas["ok"] else beta_note,
         "Fetched": betas["fetched"], "Status": "Live" if betas["ok"] else "Failed: " + betas["error"],
         "URL": BETAS_URL},
        {"Item": "USD BBB (Baa2) spread", "Source": "Damodaran ratings table (large non-financial firms)",
         "Data date": bbb["data_date"], "Fetched": bbb["fetched"],
         "Status": "Live" if bbb["ok"] else "Failed (default 1.11% used): " + bbb["error"], "URL": RATINGS_URL},
        {"Item": "Corporate tax rates", "Source": "OECD Corporate Tax Statistics",
         "Data date": oecd["data_date"], "Fetched": oecd["fetched"],
         "Status": "Live" if oecd["ok"] else "Fallback snapshot: " + oecd["error"], "URL": OECD_URL},
        {"Item": "10-year government yields", "Source": "Trading Economics",
         "Data date": te["data_date"], "Fetched": te["fetched"],
         "Status": "Live" if te["ok"] else "Fallback snapshot (8 countries): " + te["error"], "URL": TE_URL},
    ])
    st.dataframe(status, hide_index=True, width="stretch",
                 column_config={"URL": st.column_config.LinkColumn("URL")})
    st.caption("Fetched data are cached for up to 6 hours (yields 1 hour); use the Refresh button "
               "in the sidebar to force a new download.")
    if not (betas["ok"] and bbb["ok"] and oecd["ok"] and te["ok"]):
        st.warning("At least one live source failed. The websites may block automated requests or "
                   "have changed layout. Use the manual overrides to enter values you trust.")

if not te["ok"]:
    st.warning("Live bond yields could not be fetched; only a small snapshot is available. "
               "Select 'Other (enter manually)' or override the yields.")
if not oecd["ok"]:
    st.info("OECD tax rates shown come from a bundled approximate snapshot (live fetch failed). "
            "Verify them or override.")

# ---- inputs summary ---------------------------------------------------------------
st.subheader("Inputs in use")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Industry unlevered beta", f"{beta_u:.3f}", "manual" if ov_beta else "fetched", delta_color="off")
c2.metric(f"Tax rate ({tax_name})", f"{tax_pct:.2f}%", "manual" if (
    ov_tax_us if tax_choice == 'United States' else ov_tax_f) else "OECD", delta_color="off")
c3.metric("USD BBB spread", f"{spread_usd_pct:.2f}%", "manual" if ov_spread else "Damodaran", delta_color="off")
c4.metric("Relevered beta", f"{res['beta_l']:.3f}", f"D/E = {res['d_e']:.2f}", delta_color="off")
c1, c2, c3, c4 = st.columns(4)
c1.metric("U.S. 10-yr yield", f"{rf_us_pct:.3f}%", "manual" if ov_y_us else "Trading Economics", delta_color="off")
c2.metric(f"{country_label} 10-yr yield", f"{rf_f_pct:.3f}%", "manual" if ov_y_f else "Trading Economics", delta_color="off")
c3.metric("Equity risk premium", f"{erp_in:.2f}%")
c4.metric("Debt / Equity share", f"{debt_in}% / {100 - debt_in}%")

# ---- results side by side ----------------------------------------------------------
st.subheader("Results")
foreign_ccy = f"{country_label} currency"
col_a, col_b = st.columns(2)
with col_a:
    st.markdown("#### 🇺🇸 USD")
    a1, a2, a3 = st.columns(3)
    a1.metric("Cost of equity", pct(res["re_usd"]))
    a2.metric("Cost of debt (pre-tax)", pct(res["rd_usd"]), f"after-tax {pct(res['rd_usd_at'])}", delta_color="off")
    a3.metric("WACC", pct(res["wacc_usd"]))
with col_b:
    st.markdown(f"#### 🌐 {foreign_ccy}")
    b1, b2, b3 = st.columns(3)
    b1.metric("Cost of equity", pct(res["re_f"]))
    b2.metric("Cost of debt (pre-tax)", pct(res["rd_f"]), f"after-tax {pct(res['rd_f_at'])}", delta_color="off")
    b3.metric("WACC", pct(res["wacc_f"]))

results_tbl = pd.DataFrame({
    "Metric": ["Risk-free rate (10-yr gov. yield)", "Unlevered beta", "Relevered beta", "Equity risk premium",
               "Cost of equity", "BBB spread", "Pre-tax cost of debt", "Tax rate", "After-tax cost of debt",
               "Equity weight", "Debt weight", "WACC"],
    "USD": [pct(res["rf_us"]), f"{res['beta_u']:.4f}", f"{res['beta_l']:.4f}", pct(res["erp"]),
            pct(res["re_usd"]), pct(res["spread_usd"]), pct(res["rd_usd"]), pct(res["tax"]),
            pct(res["rd_usd_at"]), pct(res["equity_share"]), pct(res["debt_share"]), pct(res["wacc_usd"])],
    foreign_ccy: [pct(res["rf_f"]), f"{res['beta_u']:.4f}", f"{res['beta_l']:.4f}", pct(res["erp"]),
                  pct(res["re_f"]), pct(res["spread_f"]) + (" (manual)" if spread_f_pct is not None else " (auto)"),
                  pct(res["rd_f"]), pct(res["tax"]), pct(res["rd_f_at"]),
                  pct(res["equity_share"]), pct(res["debt_share"]), pct(res["wacc_f"])],
})
st.dataframe(results_tbl, hide_index=True, width="stretch")
st.caption(f"Differential WACC ({foreign_ccy} minus USD): {(res['wacc_f'] - res['wacc_usd']) * 10000:,.0f} bps.")

# ---- assumptions ---------------------------------------------------------------------
with st.expander("📘 Main assumptions and formulas", expanded=True):
    st.markdown(f"""
**Same project, two currencies.** Beta, equity risk premium, debt share and tax rate are identical in both
currencies; only the nominal rates differ because of the inflation/interest-rate differential.

1. **Relevered beta** (Hamada): `β_L = β_U × (1 + (1 − t) × D/E)`, with `D/E = D% / (1 − D%)`.
   The industry *unlevered* beta (Damodaran, not cash-corrected) is relevered at the target debt share.
2. **USD cost of equity**: `Re_USD = rf_US + β_L × ERP`, where `rf_US` is the U.S. 10-year Treasury yield.
3. **USD cost of debt**: `Rd_USD = rf_US + BBB spread` (Baa2/BBB, large non-financial firms). After-tax: `Rd × (1 − t)`.
4. **WACC**: `WACC = E% × Re + D% × Rd × (1 − t)`.
5. **Foreign currency** (interest-rate / inflation-differential parity):
   `r_F = (1 + r_USD) × (1 + rf_F) / (1 + rf_US) − 1`, applied to the cost of equity and the cost of debt.
   `rf_F` is the selected country's 10-year government yield.
6. **Foreign BBB spread** (auto): `s_F = Rd_F − rf_F = s_USD × (1 + rf_F)/(1 + rf_US)`.
   If you switch on the manual override, `Rd_F = rf_F + s_F(manual)` is used instead.
7. **Tax rate**: one project tax rate ({tax_name}: **{tax_pct:.2f}%**) is used for both currencies.
   OECD figures are combined (central + sub-central) statutory rates.

**Caveats.** Government yields are treated as the risk-free rate in each currency (any sovereign default
risk in high-yield countries is implicitly passed through to the foreign cost of capital); ERP is a
single global input; industry betas are U.S.-based averages.
""")

# ---- all-industry comparison --------------------------------------------------------
st.subheader("All-industry comparison")
st.caption("Uses the current ERP, debt share, tax rate, yields and spreads. Fetched industry betas "
           "are used for every industry (the beta override applies only to the selected industry above).")
rows = []
for _, r_ in beta_df.iterrows():
    bu = beta_u if r_["Industry"] == industry else float(r_["Unlevered beta"])
    o = cost_of_capital(bu, tax_pct / 100, debt_in / 100, erp_in / 100, rf_us_pct / 100, rf_f_pct / 100,
                        spread_usd_pct / 100, None if spread_f_pct is None else spread_f_pct / 100)
    rows.append({
        "Industry": r_["Industry"], "Firms": r_["Firms"], "Unlevered beta": bu,
        "Relevered beta": o["beta_l"],
        "Cost of equity USD (%)": o["re_usd"] * 100, f"Cost of equity {country_label} (%)": o["re_f"] * 100,
        "WACC USD (%)": o["wacc_usd"] * 100, f"WACC {country_label} (%)": o["wacc_f"] * 100,
    })
all_df = pd.DataFrame(rows).sort_values("WACC USD (%)", ascending=False).reset_index(drop=True)
fmt = {c: "{:.2f}" for c in all_df.columns if "(%)" in c}
fmt.update({"Unlevered beta": "{:.3f}", "Relevered beta": "{:.3f}"})
st.dataframe(all_df.style.format(fmt, na_rep="-"), hide_index=True, width="stretch", height=420)
chart_df = all_df.set_index("Industry")[["WACC USD (%)", f"WACC {country_label} (%)"]]
st.bar_chart(chart_df, height=380)

# ---- downloads -----------------------------------------------------------------------
st.subheader("Download results")
assump_df = pd.DataFrame({
    "Assumption": ["Industry", "Country", "Project tax rate used", "Tax rate (%)", "Unlevered beta",
                   "Beta source", "ERP (%)", "Debt share (%)", "U.S. 10-yr yield (%)",
                   "Selected-country 10-yr yield (%)", "USD BBB spread (%)", "Foreign spread (%)",
                   "Foreign spread mode", "Betas data date", "Spread data date", "Tax data date",
                   "Yield data date", "Generated (UTC)"],
    "Value": [industry, country_label, tax_name, round(tax_pct, 4), round(beta_u, 4),
              "manual" if ov_beta else "Damodaran", erp_in, debt_in, rf_us_pct, rf_f_pct,
              spread_usd_pct, round(res["spread_f"] * 100, 4),
              "manual" if spread_f_pct is not None else "auto", betas["data_date"], bbb["data_date"],
              oecd["data_date"], te["data_date"], now_utc()],
})
d1, d2, d3 = st.columns(3)
d1.download_button("⬇️ Results (CSV)", results_tbl.to_csv(index=False).encode(),
                   "cost_of_capital_results.csv", "text/csv", width="stretch")
d2.download_button("⬇️ All industries (CSV)", all_df.to_csv(index=False).encode(),
                   "cost_of_capital_all_industries.csv", "text/csv", width="stretch")
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as xw:
    results_tbl.to_excel(xw, sheet_name="Results", index=False)
    assump_df.to_excel(xw, sheet_name="Assumptions", index=False)
    all_df.to_excel(xw, sheet_name="All industries", index=False)
    status.to_excel(xw, sheet_name="Sources", index=False)
d3.download_button("⬇️ Full workbook (Excel)", buf.getvalue(), "cost_of_capital.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   width="stretch")
st.caption("Educational tool. Verify inputs before relying on the results.")
