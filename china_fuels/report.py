"""
China fuels daily — SunSirs spots + JLC indie cracks email.

Reads landing tables from sibling oil_data_ingest (no scrape / no BBG here).

Usage (from china_fuels/ or via run_daily.bat):
  python report.py --save-only
  python report.py
"""

from __future__ import annotations

import argparse
import base64
import logging
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CODING = REPO.parent
sys.path.insert(0, str(REPO))

from sender import send_email  # noqa: E402

DEFAULT_PRICES = (
    CODING / "oil_data_ingest" / "data" / "processed" / "sunsirs" / "prices.parquet"
)
DEFAULT_CRACKS = (
    CODING / "oil_data_ingest" / "data" / "processed" / "jlc" / "cracks.parquet"
)
SAVE_DIR = HERE / "scratch"
CRACK_PRODUCT_ORDER = ("gasoline", "diesel")

# First weeks: you only. Add teammates later as a comma-separated list.
RECIPIENTS = "luiscarlos.gaitan@jainglobal.com"
RECIPIENTS_CC: str | None = None
FROM_ADDRESS = "oil.research.desk@jainglobal.com"

PRODUCT_ORDER = ("gasoline", "diesel", "fuel_oil", "lpg")

_EMAIL_CSS = """
<style>
* { box-sizing: border-box; }
body {
  font-family: Calibri, Arial, sans-serif;
  margin: 0; padding: 20px;
  background: #ffffff; color: #1a1a2e;
  font-size: 13px; max-width: 920px;
}
h1 { font-size: 17px; margin: 0 0 4px 0; color: #1a1a2e; }
h2 { font-size: 13px; margin: 0 0 14px 0; font-weight: normal; color: #555; }
h3 { font-size: 14px; margin: 18px 0 8px 0; color: #1a1a2e; }
p.headline { font-size: 14px; margin: 0 0 14px 0; line-height: 1.35; }
p.footnote { font-size: 11px; color: #666; margin: 6px 0 0 0; }
table.score {
  border-collapse: collapse; width: 100%;
  font-size: 12px; margin: 0 0 8px 0;
}
table.score th, table.score td {
  border: 1px solid #d0d0d0; padding: 5px 8px; text-align: right;
}
table.score th { background: #f3f3f6; text-align: center; }
table.score td.left, table.score th.left { text-align: left; }
table.score td.pos { color: #0a7a2f; }
table.score td.neg { color: #b00020; }
.section.chart { margin: 10px 0 16px 0; }
hr.divider { border: none; border-top: 1px solid #ddd; margin: 18px 0; }
</style>
"""


def _silence_loggers() -> None:
    for name in ("plotly", "kaleido", "urllib3", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)


def load_prices(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing landing table: {path}\n"
            "Run oil_data_ingest scripts/fetch_sunsirs.py first."
        )
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["product", "date"]).reset_index(drop=True)


def load_cracks(path: Path) -> pd.DataFrame:
    """Optional — report still sends if cracks file is missing."""
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["product", "date"]).reset_index(drop=True)


def _series_scorecard(
    frame: pd.DataFrame,
    *,
    value_col: str,
    unit_col: str,
    product_order: tuple[str, ...],
    default_unit: str,
) -> pd.DataFrame:
    rows = []
    for product, g in frame.groupby("product", sort=True):
        g = g.sort_values("date")
        last = g.iloc[-1]
        level = float(last[value_col])
        prev = float(g.iloc[-2][value_col]) if len(g) >= 2 else float("nan")
        week = float(g.iloc[-6][value_col]) if len(g) >= 6 else float("nan")
        rows.append(
            {
                "product": product,
                "as_of": last["date"],
                "price": level,
                "unit": last.get(unit_col, default_unit) if unit_col in g.columns else default_unit,
                "dod": level - prev if pd.notna(prev) else float("nan"),
                "dod_pct": (level / prev - 1.0) * 100.0
                if pd.notna(prev) and prev
                else float("nan"),
                "d5": level - week if pd.notna(week) else float("nan"),
                "d5_pct": (level / week - 1.0) * 100.0
                if pd.notna(week) and week
                else float("nan"),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    cat = pd.Categorical(out["product"], categories=list(product_order), ordered=True)
    out = out.assign(product=cat).sort_values("product").reset_index(drop=True)
    out["product"] = out["product"].astype(str)
    return out


def scorecard(prices: pd.DataFrame) -> pd.DataFrame:
    return _series_scorecard(
        prices,
        value_col="price",
        unit_col="unit",
        product_order=PRODUCT_ORDER,
        default_unit="RMB/mt",
    )


def cracks_scorecard(cracks: pd.DataFrame) -> pd.DataFrame:
    if cracks.empty:
        return pd.DataFrame()
    return _series_scorecard(
        cracks,
        value_col="value",
        unit_col="unit",
        product_order=CRACK_PRODUCT_ORDER,
        default_unit="USD/bbl",
    )


def slate_spreads(prices: pd.DataFrame) -> pd.DataFrame:
    wide = (
        prices.pivot_table(index="date", columns="product", values="price", aggfunc="last")
        .sort_index()
    )
    pairs = [
        ("gasoline - diesel", "gasoline", "diesel"),
        ("diesel - fuel_oil", "diesel", "fuel_oil"),
        ("gasoline - lpg", "gasoline", "lpg"),
    ]
    frames = []
    for label, a, b in pairs:
        if a not in wide.columns or b not in wide.columns:
            continue
        s = (wide[a] - wide[b]).dropna()
        if s.empty:
            continue
        level = float(s.iloc[-1])
        prev = float(s.iloc[-2]) if len(s) >= 2 else float("nan")
        frames.append(
            {
                "spread": label,
                "as_of": s.index[-1],
                "level": level,
                "dod": level - prev if pd.notna(prev) else float("nan"),
            }
        )
    return pd.DataFrame(frames)


def _fmt_num(x: float, signed: bool = False) -> str:
    if pd.isna(x):
        return "—"
    return f"{x:+,.2f}" if signed else f"{x:,.2f}"


def _fmt_pct(x: float) -> str:
    if pd.isna(x):
        return "—"
    return f"{x:+.2f}%"


def _delta_class(x: float) -> str:
    if pd.isna(x) or x == 0:
        return ""
    return "pos" if x > 0 else "neg"


def headline(sc: pd.DataFrame) -> str:
    if sc.empty:
        return "No SunSirs prices available."
    as_of = pd.Timestamp(sc["as_of"].max()).strftime("%d %b %Y")
    movers = sc.dropna(subset=["dod"]).copy()
    if movers.empty:
        parts = [
            f"{r.product} {r.price:,.0f}"
            for r in sc.itertuples(index=False)
        ]
        return f"As of {as_of}: " + "; ".join(parts) + " RMB/mt."
    movers["abs_dod"] = movers["dod"].abs()
    top = movers.sort_values("abs_dod", ascending=False).iloc[0]
    sign = "+" if top["dod"] >= 0 else ""
    text = (
        f"As of {as_of}: largest DoD move {top['product']} "
        f"{sign}{top['dod']:.0f} RMB/mt ({_fmt_pct(top['dod_pct'])})"
    )
    by_prod = sc.set_index("product")["price"]
    if {"gasoline", "diesel"}.issubset(by_prod.index):
        text += (
            f"; gasoline {by_prod['gasoline']:,.0f}, "
            f"diesel {by_prod['diesel']:,.0f} RMB/mt."
        )
    else:
        text += "."
    return text


def scorecard_html(sc: pd.DataFrame) -> str:
    rows = []
    for r in sc.itertuples(index=False):
        rows.append(
            "<tr>"
            f'<td class="left">{r.product}</td>'
            f"<td>{pd.Timestamp(r.as_of).strftime('%Y-%m-%d')}</td>"
            f"<td>{_fmt_num(r.price)}</td>"
            f'<td class="{_delta_class(r.dod)}">{_fmt_num(r.dod, signed=True)}</td>'
            f'<td class="{_delta_class(r.dod_pct)}">{_fmt_pct(r.dod_pct)}</td>'
            f'<td class="{_delta_class(r.d5)}">{_fmt_num(r.d5, signed=True)}</td>'
            f'<td class="{_delta_class(r.d5_pct)}">{_fmt_pct(r.d5_pct)}</td>'
            "</tr>"
        )
    body = "\n".join(rows)
    return f"""
<table class="score">
  <thead>
    <tr>
      <th class="left">Product</th><th>As of</th><th>Price</th>
      <th>DoD</th><th>DoD %</th><th>5d</th><th>5d %</th>
    </tr>
  </thead>
  <tbody>
{body}
  </tbody>
</table>
<p class="footnote">Prices in RMB/mt (SunSirs UK spot pages). 5d = vs print 5 sessions earlier on the landing table.</p>
"""


def spreads_html(sp: pd.DataFrame) -> str:
    if sp.empty:
        return "<p class=\"footnote\">Spreads need overlapping product dates.</p>"
    rows = []
    for r in sp.itertuples(index=False):
        rows.append(
            "<tr>"
            f'<td class="left">{r.spread}</td>'
            f"<td>{pd.Timestamp(r.as_of).strftime('%Y-%m-%d')}</td>"
            f"<td>{_fmt_num(r.level)}</td>"
            f'<td class="{_delta_class(r.dod)}">{_fmt_num(r.dod, signed=True)}</td>'
            "</tr>"
        )
    body = "\n".join(rows)
    return f"""
<table class="score">
  <thead>
    <tr>
      <th class="left">Spread</th><th>As of</th><th>Level</th><th>DoD</th>
    </tr>
  </thead>
  <tbody>
{body}
  </tbody>
</table>
<p class="footnote">Simple slate spreads in RMB/mt — not a full crack (no crude, no FX).</p>
"""


def fig_to_img_tag(fig: go.Figure, width: int = 860, height: int = 360) -> str:
    img_bytes = fig.to_image(format="png", width=width, height=height, scale=2)
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return (
        f'<img src="data:image/png;base64,{b64}" '
        f'width="{width}" style="max-width:100%;display:block;">'
    )


def levels_figure(prices: pd.DataFrame) -> go.Figure:
    fig = px.line(
        prices,
        x="date",
        y="price",
        color="product",
        markers=True,
        category_orders={"product": [p for p in PRODUCT_ORDER if p in set(prices["product"])]},
        title="SunSirs China fuel spots",
        labels={"price": "RMB/mt", "date": "Date", "product": "Product"},
    )
    fig.update_layout(
        legend_title_text="",
        margin=dict(l=40, r=20, t=50, b=40),
        template="plotly_white",
    )
    return fig


def cracks_figure(cracks: pd.DataFrame) -> go.Figure | None:
    if cracks.empty:
        return None
    # Chart last ~90 sessions for readability even after a 1y backfill.
    latest = cracks["date"].max()
    window = cracks[cracks["date"] >= latest - pd.Timedelta(days=120)].copy()
    fig = px.line(
        window,
        x="date",
        y="value",
        color="product",
        markers=True,
        category_orders={
            "product": [p for p in CRACK_PRODUCT_ORDER if p in set(window["product"])]
        },
        title="JLC indie refinery cracks (USD/bbl)",
        labels={"value": "USD/bbl", "date": "Date", "product": "Product"},
    )
    fig.update_layout(
        legend_title_text="",
        margin=dict(l=40, r=20, t=50, b=40),
        template="plotly_white",
    )
    fig.add_hline(y=0, line_width=1, line_dash="dot", line_color="#888")
    return fig


def cracks_html(sc: pd.DataFrame) -> str:
    if sc.empty:
        return (
            "<p class=\"footnote\">JLC cracks landing table missing — "
            "run <code>oil_data_ingest/scripts/fetch_jlc_cracks.py</code>.</p>"
        )
    rows = []
    for r in sc.itertuples(index=False):
        rows.append(
            "<tr>"
            f'<td class="left">{r.product}</td>'
            f"<td>{pd.Timestamp(r.as_of).strftime('%Y-%m-%d')}</td>"
            f"<td>{_fmt_num(r.price)}</td>"
            f'<td class="{_delta_class(r.dod)}">{_fmt_num(r.dod, signed=True)}</td>'
            f'<td class="{_delta_class(r.dod_pct)}">{_fmt_pct(r.dod_pct)}</td>'
            f'<td class="{_delta_class(r.d5)}">{_fmt_num(r.d5, signed=True)}</td>'
            f'<td class="{_delta_class(r.d5_pct)}">{_fmt_pct(r.d5_pct)}</td>'
            "</tr>"
        )
    body = "\n".join(rows)
    return f"""
<table class="score">
  <thead>
    <tr>
      <th class="left">Product</th><th>As of</th><th>USD/bbl</th>
      <th>DoD</th><th>DoD %</th><th>5d</th><th>5d %</th>
    </tr>
  </thead>
  <tbody>
{body}
  </tbody>
</table>
<p class="footnote">JLC via Bloomberg: ex-refinery price after tax − mainstream imported crude cost
(CHPZIRGC / CHPZIRDC Index). Independent refineries, main imported grades.</p>
"""


def spreads_figure(prices: pd.DataFrame) -> go.Figure | None:
    wide = (
        prices.pivot_table(index="date", columns="product", values="price", aggfunc="last")
        .sort_index()
    )
    series = {}
    if {"gasoline", "diesel"}.issubset(wide.columns):
        series["gasoline - diesel"] = wide["gasoline"] - wide["diesel"]
    if {"diesel", "fuel_oil"}.issubset(wide.columns):
        series["diesel - fuel_oil"] = wide["diesel"] - wide["fuel_oil"]
    if {"gasoline", "lpg"}.issubset(wide.columns):
        series["gasoline - lpg"] = wide["gasoline"] - wide["lpg"]
    if not series:
        return None
    long = (
        pd.DataFrame(series)
        .reset_index()
        .melt(id_vars="date", var_name="spread", value_name="rmb_mt")
        .dropna()
    )
    fig = px.line(
        long,
        x="date",
        y="rmb_mt",
        color="spread",
        markers=True,
        title="Fuel slate spreads",
        labels={"rmb_mt": "RMB/mt", "date": "Date", "spread": "Spread"},
    )
    fig.update_layout(
        legend_title_text="",
        margin=dict(l=40, r=20, t=50, b=40),
        template="plotly_white",
    )
    return fig


def build_html(
    prices: pd.DataFrame,
    prices_path: Path,
    cracks: pd.DataFrame,
    cracks_path: Path,
) -> tuple[str, str]:
    sc = scorecard(prices)
    sp = slate_spreads(prices)
    csc = cracks_scorecard(cracks)
    as_of = pd.Timestamp(sc["as_of"].max()) if not sc.empty else pd.Timestamp.today()
    subject = f"China fuels spots | {as_of:%d %b %Y}"

    img_levels = img_spreads = img_cracks = ""
    chart_note = ""
    try:
        img_levels = fig_to_img_tag(levels_figure(prices))
        fig_sp = spreads_figure(prices)
        img_spreads = fig_to_img_tag(fig_sp) if fig_sp is not None else ""
        fig_cr = cracks_figure(cracks)
        img_cracks = fig_to_img_tag(fig_cr) if fig_cr is not None else ""
    except Exception as exc:  # kaleido / chrome headless can fail on some desktops
        chart_note = f"<p class=\"footnote\">Charts skipped ({type(exc).__name__}: {exc}).</p>"

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">{_EMAIL_CSS}</head><body>
<h1>China fuel spots — SunSirs + JLC cracks</h1>
<h2>Daily desk note &middot; data through {as_of:%d %b %Y}</h2>
<p class="headline">{headline(sc)}</p>

<h3>SunSirs scorecard (RMB/mt)</h3>
{scorecard_html(sc)}

<hr class="divider" />

<h3>Levels</h3>
<div class="section chart">{img_levels}</div>

<h3>Slate spreads</h3>
{spreads_html(sp)}
<div class="section chart">{img_spreads}</div>

<hr class="divider" />

<h3>JLC indie refinery cracks (USD/bbl)</h3>
{cracks_html(csc)}
<div class="section chart">{img_cracks}</div>
{chart_note}

<p class="footnote">Sources via oil_data_ingest (report does not scrape or call Bloomberg):
SunSirs <code>{prices_path}</code>; JLC cracks <code>{cracks_path}</code>.</p>
</body></html>"""
    return body, subject


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="China fuels daily HTML report")
    parser.add_argument(
        "--prices",
        type=Path,
        default=DEFAULT_PRICES,
        help="Path to sunsirs prices.parquet",
    )
    parser.add_argument(
        "--cracks",
        type=Path,
        default=DEFAULT_CRACKS,
        help="Path to jlc cracks.parquet",
    )
    parser.add_argument(
        "--save-only",
        action="store_true",
        help="Write HTML under scratch/; do not email",
    )
    args = parser.parse_args(argv)

    _silence_loggers()
    prices_path = args.prices.resolve()
    cracks_path = args.cracks.resolve()
    prices = load_prices(prices_path)
    cracks = load_cracks(cracks_path)
    print(f"Loaded {len(prices)} spot rows from {prices_path}", flush=True)
    print(f"Loaded {len(cracks)} crack rows from {cracks_path}", flush=True)

    body, subject = build_html(prices, prices_path, cracks, cracks_path)

    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    as_of = pd.to_datetime(prices["date"]).max().strftime("%Y%m%d")
    out = SAVE_DIR / f"china_fuels_{as_of}.html"
    out.write_text(body, encoding="utf-8")
    print(f"Wrote {out}", flush=True)

    if args.save_only:
        return 0

    ok, detail = send_email(
        recipients=RECIPIENTS,
        subject=subject,
        body=body,
        from_address=FROM_ADDRESS,
        recipients_cc=RECIPIENTS_CC,
    )
    if ok:
        print(f"Email sent to {RECIPIENTS} ({detail})", flush=True)
        return 0
    print(f"ERROR: email failed: {detail}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
