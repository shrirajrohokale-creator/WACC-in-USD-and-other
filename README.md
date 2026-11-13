# WACC-in-USD-and-other
# International Cost of Capital App

A Streamlit app that compares a project's **cost of equity, cost of debt and WACC in USD and in the
currency of a selected country**. Python 3.12 compatible.

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Data sources (fetched live; "Refresh data" button in the sidebar)

| Item | Source |
|---|---|
| Unlevered industry betas | Damodaran `betas.xls` ("Unlevered beta" column, not cash-corrected) |
| USD BBB spread | Damodaran ratings table, Baa2/BBB, large non-financial service firms |
| Corporate tax rates | OECD Corporate Tax Statistics, combined statutory rates (latest year in the table) |
| 10-year government yields | tradingeconomics.com/bonds (scraped, no API key) |

The "Data sources" panel shows each source, its data date, the time fetched and whether the fetch
was live. Results are cached (6 h; yields 1 h) until you press Refresh.

**Fallbacks.** If a site blocks scraping or changes layout: the betas file can be uploaded manually in
the sidebar; the OECD and Trading Economics steps fall back to small bundled snapshots (clearly flagged
in the UI); and any value can be overridden by hand. Always check the status panel.

## Inputs

* Industry, target country (or "Other (enter manually)"), equity risk premium, target debt share.
* Tax-rate switch: U.S. or selected-country rate, used for **both** currencies.
* Manual overrides (toggle per item): unlevered beta, U.S. tax, selected-country tax, USD BBB spread,
  U.S. and selected-country 10-year yields, and the foreign BBB spread (when off, it is auto-calculated).
* Defaults: Software (System & Application), United States, U.S. tax rate, 6.5% ERP, 30% debt.

## Formulas

Let `t` = project tax rate, `d` = debt share, `D/E = d/(1-d)`.

1. `β_L = β_U · (1 + (1 − t) · D/E)`
2. `Re_USD = rf_US + β_L · ERP`
3. `Rd_USD = rf_US + BBB spread`
4. `WACC_USD = (1 − d)·Re_USD + d·Rd_USD·(1 − t)`
5. Foreign currency, via inflation / interest-rate differential:
   `r_F = (1 + r_USD)·(1 + rf_F)/(1 + rf_US) − 1` for both equity and debt
6. Auto foreign BBB spread: `Rd_F − rf_F = s_USD·(1 + rf_F)/(1 + rf_US)`; if overridden, `Rd_F = rf_F + s_F`
7. `WACC_F = (1 − d)·Re_F + d·Rd_F·(1 − t)`

When the selected country is the United States, both columns are identical (a built-in check).

> The class formulas were not supplied with the assignment. These are the standard textbook
> versions; if your course uses a different foreign-currency conversion, edit `cost_of_capital()`
> and `fx_convert()` in `app.py` (a few lines).

## Outputs

Side-by-side results, an assumptions panel, an all-industry comparison (table + chart), and
downloads: results CSV, all-industry CSV, and a full Excel workbook (results, assumptions,
all industries, sources).

## Limitations

* Scraping depends on third-party page layouts; Trading Economics may block automated requests.
* The government yield is used as the risk-free rate in each currency, so sovereign default risk is
  passed through for high-yield countries.
* One global ERP; industry betas are U.S.-based averages.
* Educational use only.
